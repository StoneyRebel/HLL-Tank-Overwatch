import os
from dotenv import load_dotenv
import threading
import websocket
import json
from collections import defaultdict

load_dotenv()
class LogStreamManager:
    def __init__(self):
        self.ws = None
        self.is_connected = False
        self.reconnect_delay = 5
        self.subscriptions = defaultdict(lambda: None)
        self.listeners = defaultdict(list)
        self.thread = None

    def subscribe(self, action):
        self.subscriptions[action] = None

    def on(self, event, callback):
        self.listeners[event].append(callback)

    def emit(self, event, data):
        for cb in self.listeners[event]:
            cb(data)

    def connect(self):
        url = os.environ.get('CRCON_WS_URL')
        token = os.environ.get('CRCON_API_TOKEN')
        headers = { 'Authorization': f'Bearer {token}' }
        self.ws = websocket.WebSocketApp(
            url,
            header=[f"Authorization: Bearer {token}"],
            on_open=self.on_open,
            on_message=self.on_message,
            on_close=self.on_close,
            on_error=self.on_error
        )
        self.thread = threading.Thread(target=self.ws.run_forever)
        self.thread.daemon = True
        self.thread.start()

    def on_open(self, ws):
        print('🦩 Connected to CRCON WebSocket log stream.')
        self.is_connected = True
        self.send_filter_criteria()

    def on_message(self, ws, message):
        self.handle_log_message(message)

    def on_close(self, ws, *args):
        print('🦩 Connection to CRCON WebSocket closed.')
        self.is_connected = False
        threading.Timer(self.reconnect_delay, self.connect).start()

    def on_error(self, ws, error):
        print('🦩 Error with CRCON WebSocket connection:', error)

    def send_filter_criteria(self):
        if self.is_connected:
            actions = list(self.subscriptions.keys())
            filter_criteria = {
                'last_seen_id': None,
                'actions': actions
            }
            try:
                self.ws.send(json.dumps(filter_criteria))
                print('🦩 Sent filter criteria:', filter_criteria)
            except Exception as e:
                print('🦩 Failed to send filter criteria:', e)

    def handle_log_message(self, message):
        try:
            log = json.loads(message)
            action = log.get('action')
            if action in self.subscriptions:
                self.emit(action, log)
        except Exception as e:
            print('🦩 Error handling log message:', e)

    def start(self):
        self.connect()
