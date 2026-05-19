import urllib.request
import urllib.error
import json
from flask import Flask, render_template, request
from flask_socketio import SocketIO
import logging
import time
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

RYU_URL = 'http://localhost:8080'

last_topology = {"switches": {}, "hosts": {}, "links": []}

def _fetch_json(endpoint):
    try:
        url = RYU_URL + endpoint
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer SDN-Twin-Secret-Token-2026'})
        with urllib.request.urlopen(req, timeout=2) as response:
            data = response.read().decode('utf-8')
            return json.loads(data)
    except Exception:
        return None

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
    global last_topology
    
    while True:
        try:
            topology = _fetch_json('/api/topology')
            traffic = _fetch_json('/api/traffic')
            flows = _fetch_json('/api/flows')
            
            if topology:
                logs = generate_logs(last_topology, topology)
                if logs:
                    socketio.emit('event_logs', logs)

                last_topology = topology.copy()

                socketio.emit('update_data', {
                    'topology': topology,
                    'traffic': traffic or {},
                    'flows': flows or {}
                })
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

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")

if __name__ == '__main__':
    print("Starting Digital Twin Dashboard on http://localhost:5000 with Socket.IO...")
    socketio.start_background_task(target=background_thread)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
