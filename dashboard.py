import os
import urllib.request
import urllib.error
import json
from flask import Flask, render_template, request
from flask_socketio import SocketIO
import logging
import time
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins=os.environ.get('CORS_ORIGIN', 'http://localhost:5000'), async_mode='threading')
app.static_folder = 'static'

logging.getLogger('werkzeug').setLevel(logging.ERROR)

RYU_URL = 'http://localhost:8080'

last_topology = {"switches": {}, "hosts": {}, "links": []}
last_payload = None

def _fetch_json(endpoint, etag=None):
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

def generate_logs(old_topo, new_topo):
    logs = []
    
    for s in new_topo.get('switches', {}).keys():
        if s not in old_topo.get('switches', {}):
            logs.append({"type": "switch", "action": "add", "message": f"Switch s{s} connected."})
    for s in old_topo.get('switches', {}).keys():
        if s not in new_topo.get('switches', {}):
            logs.append({"type": "switch", "action": "remove", "message": f"Switch s{s} disconnected."})

    old_hosts = set(old_topo.get('hosts', {}).keys())
    new_hosts = set(new_topo.get('hosts', {}).keys())
    for h in new_hosts - old_hosts:
        host = new_topo['hosts'][h]
        ip = host.get('ipv4') or host.get('mac')
        logs.append({"type": "host", "action": "add", "message": f"Host {ip} joined at s{host.get('dpid')}."})
    for h in old_hosts - new_hosts:
        host = old_topo['hosts'][h]
        ip = host.get('ipv4') or host.get('mac')
        logs.append({"type": "host", "action": "remove", "message": f"Host {ip} left."})

    old_links = {(l['src_dpid'], l['dst_dpid']) for l in old_topo.get('links', [])}
    new_links = {(l['src_dpid'], l['dst_dpid']) for l in new_topo.get('links', [])}
    
    for link in new_links - old_links:
        logs.append({"type": "link", "action": "add", "message": f"Link added s{link[0]} - s{link[1]}."})
    for link in old_links - new_links:
        logs.append({"type": "link", "action": "remove", "message": f"Link removed s{link[0]} - s{link[1]}."})

    for log in logs:
        log['timestamp'] = time.strftime("%H:%M:%S")
    
    return logs

def background_thread():
    global last_topology, last_payload
    topo_etag = None
    traffic_etag = None
    flows_etag = None
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as executor:
        while True:
            try:
                topo_future = executor.submit(_fetch_json, '/api/topology', topo_etag)
                traffic_future = executor.submit(_fetch_json, '/api/traffic', traffic_etag)
                flows_future = executor.submit(_fetch_json, '/api/flows', flows_etag)

                topology, topo_etag = topo_future.result()
                traffic, traffic_etag = traffic_future.result()
                flows, flows_etag = flows_future.result()

                if topology:
                    logs = generate_logs(last_topology, topology)
                    if logs:
                        socketio.emit('event_logs', logs)
                    last_topology = topology
                    last_payload = {
                        'topology': topology,
                        'traffic': traffic or {},
                        'flows': flows or {}
                    }
                    socketio.emit('update_data', last_payload)
                else:
                    socketio.emit('status', {'error': 'Could not fetch data from RYU'})
            except Exception as e:
                logging.error(f"Background poll error: {e}")
                socketio.emit('status', {'error': f'Poll failed: {str(e)}'})

            time.sleep(5)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")
    if last_payload:
        socketio.emit('update_data', last_payload, to=request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")

if __name__ == '__main__':
    print("Starting Digital Twin Dashboard on http://localhost:5000 with Socket.IO...")
    socketio.start_background_task(target=background_thread)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
