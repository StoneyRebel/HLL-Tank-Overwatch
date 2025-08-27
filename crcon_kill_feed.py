import os
from dotenv import load_dotenv
from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit
from log_stream_manager import LogStreamManager
from helpers.filters import parse_args, should_show_kill

load_dotenv()
app = Flask(__name__, static_folder='public')
socketio = SocketIO(app, cors_allowed_origins="*")

filters = parse_args()
log_stream_manager = LogStreamManager()
connected_clients = set()

@app.route('/filters.json')
def filters_json():
    """Return current filters as JSON."""
    return jsonify(filters)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    """Serve static files."""
    if path != "" and os.path.exists(app.static_folder + '/' + path):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, 'index.html')

@socketio.on('connect')
def handle_connect():
    """Handle new client connection."""
    connected_clients.add(request.sid)
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnect."""
    connected_clients.discard(request.sid)
    print(f"Client disconnected: {request.sid}")

# Listen for KILL events from log stream manager

def handle_kill(log):
    if not should_show_kill(log, filters):
        return
    data = { 'type': 'KILL', 'payload': log }
    socketio.emit('kill_event', data)

log_stream_manager.on('KILL', handle_kill)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 3000))
    log_stream_manager.subscribe('KILL')
    log_stream_manager.start()
    print(f"🎯 Kill Feed Server running at http://localhost:{port}")
    print("🛠 Filters:", filters)
    socketio.run(app, host="0.0.0.0", port=port)
