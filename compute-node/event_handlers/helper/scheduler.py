import json
import time
from collections import defaultdict
from typing import Dict, List, Any
from abc import ABC, abstractmethod
import random
import logging
import asyncio

from coredis.exceptions import RedisError

logger = logging.getLogger(__name__)


class BaseScheduler(ABC):
    @abstractmethod
    def __init__(self, dependency_graph: Dict[str, Any], app_requirements: Dict[str, Dict[str, int]],
                 redis_config: Dict[str, Any]):
        pass

    @abstractmethod
    async def schedule(self) -> Dict[str, List[str]]:
        pass


class SchedulingError(Exception):
    """Base class for scheduling errors"""
    pass


class ResourceConstraintError(SchedulingError):
    """Raised when there are insufficient resources to schedule a task"""
    pass


class NodeSelectionError(SchedulingError):
    """Raised when no suitable node can be found for a task"""
    pass


class LCDWRRScheduler(BaseScheduler):
    def __init__(self, dependency_graph: Dict[str, Any], app_requirements: Dict[str, Dict[str, int]],
                 redis_config: Dict[str, Any]):
        self.dependency_graph = dependency_graph
        self.app_requirements = app_requirements
        self.redis = redis_config.get("redis-client")
        self.max_retries = 3
        self.retry_delay = 5  # seconds

    @staticmethod
    def calculate_load(node: Dict) -> float:
        cpu_load = node['resource_data']['cpu']['used_percent'] / 100
        memory_load = node['resource_data']['memory']['used_percent'] / 100
        return (cpu_load * 0.5) + (memory_load * 0.5)

    @staticmethod
    def calculate_available_resources(node: Dict) -> Dict[str, float]:
        cpu_available = node['resource_data']['cpu']['total'] * (1 - node['resource_data']['cpu']['used_percent'] / 100)
        memory_available = node['resource_data']['memory']['total'] * (
                1 - node['resource_data']['memory']['used_percent'] / 100)
        return {
            'cpu': cpu_available,
            'memory': memory_available
        }

    def select_node(self, app_id: str, node_resources: Dict[str, List[Dict]]) -> str:
        eligible_nodes = []
        for node in node_resources['rows']:
            available_resources = self.calculate_available_resources(node['doc'])
            if (self.app_requirements[app_id]['cpu'] <= available_resources['cpu'] and
                    self.app_requirements[app_id]['memory'] <= available_resources['memory']):
                eligible_nodes.append(node)

        if not eligible_nodes:
            raise NodeSelectionError(f"No eligible nodes found for application {app_id}")

        least_loaded_nodes = sorted(eligible_nodes, key=lambda x: self.calculate_load(x['doc']))[:3]
        weights = [1 / (self.calculate_load(node['doc']) + 0.1) for node in least_loaded_nodes]
        selected_node = random.choices(least_loaded_nodes, weights=weights)[0]

        return selected_node['id']

    def update_node_resources(self, node_resources: Dict[str, List[Dict]], node_id: str, app_id: str):
        for node in node_resources['rows']:
            if node['id'] == node_id:
                latest_data = node['doc']['resource_data']
                cpu_used = latest_data['cpu']['used_percent']
                memory_used = latest_data['memory']['used_percent']
                cpu_total = latest_data['cpu']['total']
                memory_total = latest_data['memory']['total']

                cpu_used_new = cpu_used + (self.app_requirements[app_id]['cpu'] / cpu_total * 100)
                memory_used_new = memory_used + (self.app_requirements[app_id]['memory'] / memory_total * 100)

                new_resource_data = latest_data.copy()
                new_resource_data['cpu']['used_percent'] = min(cpu_used_new, 100)
                new_resource_data['memory']['used_percent'] = min(memory_used_new, 100)

                # Update the in-memory representation
                node['doc']['resource_data'] = new_resource_data
                return True
        return False

    async def get_latest_node_data(self):
        node_resources = {'rows': []}

        try:
            # Use scan_iter to get all keys matching the pattern across the cluster
            async for key in self.redis.scan_iter(match='resources_*'):
                node_id = key.split('_', 1)[1]
                redis_data = await self.redis.get(key)
                if redis_data:
                    resource_data = json.loads(redis_data)
                    node_resources['rows'].append({
                        'id': node_id,
                        'doc': {'resource_data': resource_data}
                    })
                    logger.debug(f"Data for node {key} fetched from Redis")
                else:
                    logger.warning(f"No resource data available in Redis for node {key}")

            logger.debug(f"Total nodes found in Redis Cluster: {len(node_resources['rows'])}")

        except RedisError as e:
            logger.error(f"Error accessing Redis Cluster: {str(e)}")

        return node_resources

    async def schedule(self) -> Dict[str, Any]:
        node_resources = await self.get_latest_node_data()
        node_schedule = {node['id']: [] for node in node_resources['rows']}
        unscheduled_tasks = []

        sorted_apps, levels = self.topological_sort_with_levels()
        logger.debug(f"Topologically sorted apps with levels: {levels}")

        level_info = {}
        for level, apps in levels.items():
            level_info[level] = []
            for app_id in apps:
                scheduled = False
                for attempt in range(self.max_retries):
                    try:
                        selected_node = self.select_node(app_id, node_resources)
                        node_schedule[selected_node].append(app_id)
                        self.update_node_resources(node_resources, selected_node, app_id)
                        level_info[level].append({
                            "app_id": app_id,
                            "node": selected_node,
                            "next": self.dependency_graph['nodes'][app_id].get('next', [])
                        })
                        logger.debug(f"Scheduled {app_id} on node {selected_node}")
                        scheduled = True
                        break
                    except NodeSelectionError as e:
                        logger.warning(f"Attempt {attempt + 1} failed for {app_id}: {str(e)}")
                        if attempt < self.max_retries - 1:
                            await asyncio.sleep(self.retry_delay)
                            node_resources = await self.get_latest_node_data()
                        else:
                            logger.error(f"Failed to schedule {app_id} after {self.max_retries} attempts")

                if not scheduled:
                    unscheduled_tasks.append(app_id)

        if unscheduled_tasks:
            logger.error(f"Unable to schedule tasks: {unscheduled_tasks}")
            raise ResourceConstraintError(f"Failed to schedule {len(unscheduled_tasks)} tasks: {unscheduled_tasks}")

        return {
            "node_schedule": node_schedule,
            "level_info": level_info,
            "timestamp": int(time.time())
        }

    def topological_sort_with_levels(self):
        graph = {node: self.dependency_graph['nodes'][node].get('next', []) for node in self.dependency_graph['nodes']}
        in_degree = {node: 0 for node in graph}
        for node in graph:
            for neighbor in graph[node]:
                in_degree[neighbor] += 1

        queue = [(node, 0) for node in in_degree if in_degree[node] == 0]
        result = []
        levels = defaultdict(list)

        while queue:
            node, level = queue.pop(0)
            result.append(node)
            levels[level].append(node)

            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append((neighbor, level + 1))

        if len(result) != len(self.dependency_graph['nodes']):
            raise ValueError("Graph has a cycle")

        return result, levels


def create_scheduler(scheduler_type: str, dependency_graph: Dict, app_requirements: Dict,
                     redis_config: Dict) -> BaseScheduler:
    if scheduler_type == "lcdwrr":
        return LCDWRRScheduler(dependency_graph, app_requirements, redis_config)
    else:
        raise ValueError(f"Unknown scheduler type: {scheduler_type}")
