import boto3
import logging
import json
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

session = boto3.Session()

ec2_client = session.client('ec2')
cloudwatch_client = session.client('cloudwatch')
s3_client = session.client('s3')

def collect_detailed_volumes(filter_function):
    try:
        volumes = ec2_client.describe_volumes()['Volumes']
    except Exception as e:
        logging.error(f"Error fetching EBS volumes: {e}")
        return []

    filtered_volumes = filter_function(volumes)

    detailed_info = []
    for volume in filtered_volumes:
        volume_info = {
            'ebs_id': volume['VolumeId'],
            'ebs_name': next((tag['Value'] for tag in volume.get('Tags', []) if tag['Key'] == 'Name'), None),
            'az': volume['AvailabilityZone'],
            'ebs_type': volume['VolumeType'],
            'ebs_size': volume['Size'],
            'ebs_iops': volume.get('Iops', None),
            'ebs_throughput': volume.get('Throughput', None) #valid only for gp3
        }
        detailed_info.append(volume_info)

    return detailed_info

def collect_detailed_snapshots():
    try:
        snapshots = ec2_client.describe_snapshots(OwnerIds=['self'])['Snapshots']
    except Exception as e:
        logging.error(f"Error fetching EBS snapshots: {e}")
        return []

    non_encrypted_snapshots = [snap for snap in snapshots if not snap['Encrypted']]
    detailed_info = []
    for snapshot in non_encrypted_snapshots:
        snapshot_info = {
            'snapshot_id': snapshot['SnapshotId'],
            'snapshot_name': next((tag['Value'] for tag in snapshot.get('Tags', []) if tag['Key'] == 'Name'), None),
            'region': session.region_name,
            'snapshot_size': snapshot['VolumeSize'],
            'snapshot_creation_date': snapshot['StartTime'].strftime('%Y-%m-%d %H:%M:%S')
        }
        detailed_info.append(snapshot_info)
    return detailed_info

def fetch_volume_metrics(volume_id):
    metric_names = ['VolumeReadBytes', 'VolumeWriteBytes', 'VolumeReadOps', 'VolumeWriteOps']
    metrics_data = {}
    
    for metric_name in metric_names:
        try:
            response = cloudwatch_client.get_metric_data(
                MetricDataQueries=[
                    {
                        'Id': f"m{metric_name}Query",
                        'MetricStat': {
                            'Metric': {
                                'Namespace': 'AWS/EBS',
                                'MetricName': metric_name,
                                'Dimensions': [{'Name': 'VolumeId', 'Value': volume_id}]
                            },
                            'Period': 300,
                            'Stat': 'Average'
                        },
                        'ReturnData': True
                    }
                ],
                StartTime=datetime.now() - timedelta(days=1),
                EndTime=datetime.now()
            )
            
            data_points = response['MetricDataResults'][0]['Values']
            if data_points:
                metrics_data[metric_name] = sum(data_points) / len(data_points)
            else:
                metrics_data[metric_name] = 0
        except Exception as e:
            logging.error(f"Error fetching {metric_name} for volume {volume_id}: {e}")
            metrics_data[metric_name] = None

    return metrics_data

def generate_consolidated_report():
    unattached_volumes = collect_detailed_volumes(lambda vols: [vol for vol in vols if not vol['Attachments']])
    for volume in unattached_volumes:
        metrics = fetch_volume_metrics(volume['ebs_id'])
        volume.update(metrics)
    
    non_encrypted_volumes = collect_detailed_volumes(lambda vols: [vol for vol in vols if not vol['Encrypted']])
    non_encrypted_snapshots = collect_detailed_snapshots()

    report = {
        'summary': {
            'unattached_volumes_count': len(unattached_volumes),
            'non_encrypted_volumes_count': len(non_encrypted_volumes),
            'non_encrypted_snapshots_count': len(non_encrypted_snapshots)
        },
        'unattached_volumes': unattached_volumes,
        'non_encrypted_volumes': non_encrypted_volumes,
        'non_encrypted_snapshots': non_encrypted_snapshots
    }

    return report


def save_to_s3(data, filename, bucket_name):
    try:
        s3_client.put_object(Bucket=bucket_name, Key=filename, Body=json.dumps(data))
        logging.info(f"Saved {filename} to S3 bucket {bucket_name}.")


    except Exception as e:
        logging.error(f"Error saving {filename} to S3 bucket {bucket_name}: {e}")

def lambda_handler(event, context):

    total_report = generate_consolidated_report()
    bucket_name = "vol-metrics"
    current_datetime = datetime.now().strftime('%d-%m-%Y_%H-%M-%S')
    filename = f"metrics_report_{current_datetime}.json"
    save_to_s3(total_report, filename, bucket_name)

    return {
        'statusCode': 200,
        'body': json.dumps('Report generated successfully!')
    }