"""
poller.py

This module orchestrates the background polling thread for the Flask dashboard.
It continuously queries the physical Ryu controller for topology and traffic updates
and broadcasts them to all connected WebSocket clients.
"""
import time
import urllib.request
import urllib.error
import json
import os
import logging
from concurrent.futures import ThreadPoolExecutor

from utils import generate_logs

RYU_URL = 'http://localhost:8080'

last_topology = {"switches": {}, "hosts": {}, "links": []}
last_payload = None

def _fetch_json(endpoint, etag=None):
    """Fetches JSON data from the Ryu API using ETag caching."""
    try:
        url = RYU_URL + endpoint
        token = os.environ.get('SDN_TWIN_AUTH_TOKEN', 'Bearer SDN-Twin-Secret-Token-2026')
        req = urllib.request.Request(url, headers={'Authorization': token})
        if etag:
            req.add_header('If-None-Match', etag)
        with urllib.request.urlopen(req, timeout=2) as response:
            data = response.read().decode('utf-8')
            return json.loads(data), response.getheader('ETag')
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        return None, None
    except Exception:
        return None, None

def background_thread(socketio):
    """Infinitely polls APIs and pushes diffs to Socket.IO clients."""
    global last_topology, last_payload
    topo_etag = None
    traffic_etag = None
    flows_etag = None

    with ThreadPoolExecutor(max_workers=3) as executor:
        while True:
            try:
                topo_future = executor.submit(_fetch_json, '/api/topology', topo_etag)
                traffic_future = executor.submit(_fetch_json, '/api/traffic', traffic_etag)
                flows_future = executor.submit(_fetch_json, '/api/flows', flows_etag)

                topology, topo_etag = topo_future.result()
                traffic, traffic_etag = traffic_future.result()
                flows, flows_etag = flows_future.result()

                if topology is None and topo_etag is None:
                    socketio.emit('status', {'error': 'Could not fetch data from RYU'})
                else:
                    current_topo = topology if topology else last_topology
                    if topology:
                        logs = generate_logs(last_topology, topology)
                        if logs:
                            socketio.emit('event_logs', logs)
                        last_topology = topology
                    
                    last_payload = {
                        'topology': current_topo,
                        'traffic': traffic or {},
                        'flows': flows or {}
                    }
                    socketio.emit('update_data', last_payload)
            except Exception as e:
                logging.error(f"Background poll error: {e}")
                socketio.emit('status', {'error': f'Poll failed: {str(e)}'})

            time.sleep(1)

def get_last_payload():
    """Accessor for the most recent payload to serve immediately to newly connected clients."""
    return last_payload
