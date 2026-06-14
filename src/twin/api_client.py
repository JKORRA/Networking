"""
api_client.py

This module is responsible for handling all HTTP REST API communication
with the physical Ryu SDN controller. It provides robust fetching mechanisms,
including ETag caching for performance and retry logic for fault tolerance.
"""

import urllib.request
import urllib.error
import json
import os
import time
from mininet.log import info, error

RYU_URL = 'http://localhost:8080'
TOPOLOGY_ENDPOINT = '/api/topology'
FLOWS_ENDPOINT = '/api/flows'
MAX_RETRIES = 10
RETRY_DELAY = 2

def _fetch_json(endpoint, base_url=RYU_URL, timeout=10):
    """Fetches JSON data from the specified API endpoint."""
    try:
        url = base_url + endpoint
        req = urllib.request.Request(url, headers={'Authorization': os.environ.get('SDN_TWIN_AUTH_TOKEN', 'Bearer SDN-Twin-Secret-Token-2026')})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode('utf-8')
            return json.loads(data)
    except urllib.error.URLError:
        return None
    except json.JSONDecodeError:
        return None

def _fetch_json_cached(endpoint, base_url=RYU_URL, timeout=10, etag=None):
    """Fetches JSON data using ETag caching to avoid redundant data transfer."""
    try:
        url = base_url + endpoint
        headers = {'Authorization': os.environ.get('SDN_TWIN_AUTH_TOKEN', 'Bearer SDN-Twin-Secret-Token-2026')}
        if etag:
            headers['If-None-Match'] = etag
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode('utf-8')
            return json.loads(data), response.getheader('ETag')
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "NOT_MODIFIED", etag
        return None, None
    except Exception:
        return None, None

class TopologyFetcher:
    """Handles robust topology fetching with retries and validation."""
    def __init__(self, api_url=RYU_URL):
        self.api_url = api_url
    
    def fetch_topology(self, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY, silent=False):
        """Fetches the topology with built-in retry logic and state validation."""
        if not silent:
            info(f"Fetching topology from {self.api_url}\n")
        
        for attempt in range(max_retries):
            try:
                topology = _fetch_json(TOPOLOGY_ENDPOINT, self.api_url, timeout=5)
                
                if not topology:
                    if not silent:
                        error(f'Failed to fetch topology (attempt {attempt + 1}/{max_retries})\n')
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    continue
                
                if not topology.get('switches', {}):
                    if not silent:
                        error(f'No switches in topology yet (attempt {attempt + 1}/{max_retries})\n')
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    else:
                        return topology
                
                if not topology.get('links', []):
                    if not silent:
                        error(f'WARNING: No links discovered yet\n')
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    else:
                        return topology
                
                if not silent:
                    num_switches = len(topology.get('switches', {}))
                    num_links = len(topology.get('links', []))
                    num_hosts = len(topology.get('hosts', {}))
                    info(f'Topology fetched successfully (version {topology.get("version", 0)})\n')
                    info(f'Switches: {num_switches}, Links: {num_links}, Hosts: {num_hosts}\n')
                
                return topology
                
            except Exception as e:
                if not silent:
                    error(f'Connection attempt {attempt + 1}/{max_retries} failed: {e}\n')
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
        
        if not silent:
            error('Failed to fetch topology after maximum retries\n')
        return None
