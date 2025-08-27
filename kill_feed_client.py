import socketio
import asyncio

class KillFeedClient:
    """Client to receive kill feed events from Flask/SocketIO server and forward to Discord."""
    def __init__(self, server_url):
        self.server_url = server_url
        self.sio = socketio.AsyncClient()
        self.bot = None
        self.channel_id = None

    async def run(self, bot, channel_id):
        self.bot = bot
        self.channel_id = channel_id

        @self.sio.event
        async def connect():
            print("✅ Connected to Kill Feed server")

        @self.sio.event
        async def disconnect():
            print("❌ Disconnected from Kill Feed server")

        @self.sio.on('kill_event')
        async def on_kill_event(data):
            channel = self.bot.get_channel(self.channel_id)
            if channel:
                payload = data.get('payload', {})
                killer = payload.get('killer', 'Unknown')
                victim = payload.get('victim', 'Unknown')
                weapon = payload.get('weapon', 'Unknown')
                await channel.send(f"💀 {killer} killed {victim} with {weapon}")

        await self.sio.connect(self.server_url)
        await self.sio.wait()