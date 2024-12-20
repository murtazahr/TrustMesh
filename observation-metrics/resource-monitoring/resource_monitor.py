#!/usr/bin/env python3
import time
from datetime import datetime
import pandas as pd
from kubernetes import client, config
from kubernetes.client import ApiClient
import json
import csv
from pathlib import Path


class K3sNodeMonitor:
    def __init__(self, interval_seconds=10, duration_minutes=30):
        self.interval = interval_seconds
        self.duration = duration_minutes * 60  # Convert to seconds
        self.metrics_data = []
        self.metrics_api_available = False

        # Initialize Kubernetes client
        try:
            config.load_kube_config()
        except Exception:
            # Fallback to in-cluster config if running inside cluster
            config.load_incluster_config()

        self.core_api = client.CoreV1Api()
        self.custom_api = client.CustomObjectsApi()

        # Check if metrics-server is available
        self.check_metrics_server()

    def check_metrics_server(self):
        """Check if metrics-server is available in the cluster"""
        try:
            api_resources = self.custom_api.get_api_resources(
                group="metrics.k8s.io",
                version="v1beta1"
            )
            self.metrics_api_available = True
            print("✓ metrics-server is available")
        except Exception:
            print("✗ metrics-server not found. Some metrics might be limited.")
            self.metrics_api_available = False

    def get_node_metrics(self):
        """Fetch metrics for all nodes in the cluster"""
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            nodes = self.core_api.list_node()

            for node in nodes.items:
                node_name = node.metadata.name
                metrics = {}

                # Get CPU and Memory metrics either from metrics-server or node status
                if self.metrics_api_available:
                    metrics = self._get_metrics_server_data(node_name)
                else:
                    metrics = self._get_node_status_data(node)

                # Get pod count on the node
                pod_count = self._get_pod_count(node_name)

                # Combine all metrics
                node_metrics = {
                    'timestamp': current_time,
                    'node_name': node_name,
                    'cpu_usage_percent': metrics.get('cpu_percent', 0),
                    'memory_usage_percent': metrics.get('memory_percent', 0),
                    'cpu_cores': metrics.get('cpu_cores', 0),
                    'memory_bytes': metrics.get('memory_bytes', 0),
                    'pod_count': pod_count,
                    'node_status': node.status.conditions[-1].type,
                    'kubelet_version': node.status.node_info.kubelet_version
                }

                # Add container runtime metrics if available
                runtime_metrics = self._get_container_runtime_metrics(node_name)
                node_metrics.update(runtime_metrics)

                self.metrics_data.append(node_metrics)

        except Exception as e:
            print(f"Error fetching metrics: {str(e)}")

    def _get_metrics_server_data(self, node_name):
        """Get metrics from metrics-server if available"""
        try:
            metrics = self.custom_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes"
            )

            for item in metrics.get('items', []):
                if item['metadata']['name'] == node_name:
                    cpu_usage = self.parse_quantity(item['usage']['cpu'])
                    memory_usage = self.parse_quantity(item['usage']['memory'])

                    # Get node capacity
                    node = self.core_api.read_node(node_name)
                    cpu_capacity = float(node.status.capacity['cpu'])
                    memory_capacity = self.parse_quantity(node.status.capacity['memory'])

                    # Convert CPU nanocores to cores for percentage calculation
                    cpu_cores = cpu_usage / 1e9  # Convert nanocores to cores

                    return {
                        'cpu_percent': round((cpu_cores / cpu_capacity) * 100, 2),
                        'memory_percent': round((memory_usage / memory_capacity) * 100, 2),
                        'cpu_cores': round(cpu_cores, 3),
                        'memory_bytes': memory_usage
                    }
        except Exception as e:
            print(f"Error getting metrics for node {node_name}: {str(e)}")
        return {'cpu_percent': 0, 'memory_percent': 0, 'cpu_cores': 0, 'memory_bytes': 0}

    def parse_quantity(self, quantity):
        """Parse Kubernetes quantity strings to numeric values"""
        try:
            if isinstance(quantity, (int, float)):
                return float(quantity)

            # Handle CPU units
            if quantity.endswith('n'):  # nanocores
                return float(quantity.rstrip('n'))
            elif quantity.endswith('u'):  # microcores
                return float(quantity.rstrip('u')) * 1000
            elif quantity.endswith('m'):  # millicores
                return float(quantity.rstrip('m')) * 1000000

            # Handle memory units
            multipliers = {
                'Ki': 1024,
                'Mi': 1024 ** 2,
                'Gi': 1024 ** 3,
                'Ti': 1024 ** 4,
                'Pi': 1024 ** 5,
                'K': 1000,
                'M': 1000 ** 2,
                'G': 1000 ** 3,
                'T': 1000 ** 4,
                'P': 1000 ** 5
            }

            for suffix, multiplier in multipliers.items():
                if quantity.endswith(suffix):
                    return float(quantity[:-len(suffix)]) * multiplier

            # No units - return as is
            return float(quantity)

        except (ValueError, TypeError) as e:
            print(f"Error parsing quantity '{quantity}': {str(e)}")
            return 0

    def _get_node_status_data(self, node):
        """Get basic metrics from node status when metrics-server is not available"""
        try:
            allocatable_cpu = float(node.status.allocatable['cpu'])
            allocatable_memory = self.parse_quantity(node.status.allocatable['memory'])
            capacity_cpu = float(node.status.capacity['cpu'])
            capacity_memory = self.parse_quantity(node.status.capacity['memory'])

            return {
                'cpu_percent': round(((capacity_cpu - allocatable_cpu) / capacity_cpu) * 100, 2),
                'memory_percent': round(((capacity_memory - allocatable_memory) / capacity_memory) * 100, 2),
                'cpu_cores': allocatable_cpu,
                'memory_bytes': allocatable_memory
            }
        except Exception as e:
            print(f"Error getting node status data: {str(e)}")
            return {'cpu_percent': 0, 'memory_percent': 0, 'cpu_cores': 0, 'memory_bytes': 0}

    def _get_pod_count(self, node_name):
        """Get number of pods running on the node"""
        try:
            pods = self.core_api.list_pod_for_all_namespaces(
                field_selector=f'spec.nodeName={node_name}'
            )
            return len(pods.items)
        except Exception:
            return 0

    def _get_container_runtime_metrics(self, node_name):
        """Get container runtime metrics if available"""
        try:
            node = self.core_api.read_node(node_name)
            return {
                'container_runtime': node.status.node_info.container_runtime_version,
                'os_image': node.status.node_info.os_image,
            }
        except Exception:
            return {}

    def monitor(self):
        """Start monitoring nodes for the specified duration"""
        print(f"Starting K3s node monitoring for {self.duration / 60} minutes...")
        start_time = time.time()
        iterations = 0

        while time.time() - start_time < self.duration:
            print(f"Collecting metrics... (Iteration {iterations + 1})")
            self.get_node_metrics()
            iterations += 1
            time.sleep(self.interval)

    def save_to_csv(self, filename='node_metrics.csv'):
        """Save collected metrics to CSV file"""
        df = pd.DataFrame(self.metrics_data)
        df.to_csv(filename, index=False)
        print(f"\nMetrics saved to {filename}")

        # Print summary statistics
        print("\nSummary Statistics:")
        for node in df['node_name'].unique():
            node_data = df[df['node_name'] == node]
            print(f"\nNode: {node}")
            print(f"Average CPU Usage: {node_data['cpu_usage_percent'].mean():.2f}%")
            print(f"Average Memory Usage: {node_data['memory_usage_percent'].mean():.2f}%")
            print(f"Average Pod Count: {node_data['pod_count'].mean():.0f}")
            print(f"CPU Cores: {node_data['cpu_cores'].iloc[0]:.2f}")
            print(f"Memory: {node_data['memory_bytes'].iloc[0] / (1024 ** 3):.2f} GB")


def main():
    # Create monitoring directory if it doesn't exist
    Path('monitoring').mkdir(exist_ok=True)

    # Initialize and run monitor
    monitor = K3sNodeMonitor(
        interval_seconds=10,  # Collect metrics every 10 seconds
        duration_minutes=30  # Run for 30 minutes
    )

    try:
        monitor.monitor()
    except KeyboardInterrupt:
        print("\nMonitoring stopped by user")
    finally:
        # Save metrics with timestamp in filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"monitoring/node_metrics_{timestamp}.csv"
        monitor.save_to_csv(filename)


if __name__ == "__main__":
    main()
