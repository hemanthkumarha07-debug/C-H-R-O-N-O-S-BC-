import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .engine import engine_controller
from .services import build_dashboard_snapshot


class ChronosConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer streaming live arbitrage telemetry and strike events."""

    async def connect(self):
        await self.channel_layer.group_add('ticker', self.channel_name)
        await self.accept()

        snapshot = await database_sync_to_async(build_dashboard_snapshot)(
            engine_controller.get_status()
        )
        await self.send(text_data=json.dumps({'event': 'snapshot', **snapshot}))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard('ticker', self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            return
        if message.get('action') == 'ping':
            await self.send(text_data=json.dumps({'event': 'pong'}))

    async def send_update(self, event):
        await self.send(text_data=json.dumps(event['data']))
