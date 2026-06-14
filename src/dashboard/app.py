"""
app.py

This module is the entry point for the Flask-based dashboard.
It sets up the web server, WebSocket instance, and basic routes,
while delegating background polling to the `poller.py` module.
"""
import os
import logging
from flask import Flask, render_template, request
from flask_socketio import SocketIO

from poller import background_thread, get_last_payload

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins=os.environ.get('CORS_ORIGIN', 'http://localhost:5000'), async_mode='threading')
app.static_folder = 'static'

logging.getLogger('werkzeug').setLevel(logging.ERROR)

@app.route('/')
def index():
    """Serves the main dashboard HTML."""
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    """Sends the latest known payload to newly connected clients."""
    print(f"Client connected: {request.sid}")
    payload = get_last_payload()
    if payload:
        socketio.emit('update_data', payload, to=request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    """Handles client disconnects."""
    print(f"Client disconnected: {request.sid}")

if __name__ == '__main__':
    print("Starting Digital Twin Dashboard on http://localhost:5000 with Socket.IO...")
    socketio.start_background_task(target=background_thread, socketio=socketio)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
