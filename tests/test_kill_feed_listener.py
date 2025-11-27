import asyncio
import json
import socket

import aiohttp
import pytest
from aiohttp import web

from unittest import mock

import aiohttp

from enhanced_discord_bot import KillFeedListener


def _find_free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _, port = sock.getsockname()
    sock.close()
    return port


@pytest.mark.asyncio
async def test_kill_feed_listener_sends_subscription_and_receives_logs():
    received = {"auth": None, "subscription": None}

    async def ws_handler(request: web.Request):
        received["auth"] = request.headers.get("Authorization")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # Expect subscription message from client
        sub_msg = await ws.receive_json()
        received["subscription"] = sub_msg
        # Send a single log event payload back
        await ws.send_json({
            "logs": [{
                "id": 1,
                "log": {
                    "action": "KILL",
                    "weapon": "75mm",
                    "killer": "Allied Gunner",
                    "victim": "Axis Tank",
                },
            }],
        })
        await asyncio.sleep(0.05)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws/logs", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _find_free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    url = f"ws://127.0.0.1:{port}/ws/logs"
    listener = KillFeedListener(url, "token123", backoff_base=0.01, backoff_max=0.05)
    listener.start()

    try:
        assert await listener.wait_until_connected(timeout=2)
        queue = listener.get_queue()
        event = await asyncio.wait_for(queue.get(), timeout=2)
        assert isinstance(event.payload, dict)
        assert event.payload["logs"][0]["log"]["action"] == "KILL"
        assert received["auth"] == "Bearer token123"
        assert received["subscription"]["actions"] == ["KILL"]
    finally:
        await listener.stop()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_kill_feed_listener_disables_after_auth_failures(monkeypatch):
    def fake_ws_connect(*args, **kwargs):
        raise aiohttp.ClientResponseError(
            request_info=mock.Mock(real_url="ws://example"),
            history=(),
            status=403,
            message="Forbidden",
            headers={},
        )

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", fake_ws_connect, raising=False)

    listener = KillFeedListener("ws://fake", "token123", auth_fail_limit=1, backoff_base=0.01, backoff_max=0.01)

    await listener._run()

    assert listener.disabled_reason is not None
    assert "Authentication failed" in listener.disabled_reason
