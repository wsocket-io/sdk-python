"""
wSocket Python SDK — Async WebSocket Pub/Sub client.

Usage:
    import asyncio
    from wsocket import create_client

    async def main():
        client = create_client("ws://localhost:9001", "your-api-key")
        await client.connect()

        chat = client.channel("chat")

        @chat.on_message
        def handle(data, meta):
            print(f"Received: {data} at {meta['timestamp']}")

        chat.subscribe()
        chat.publish({"text": "Hello from Python!"})

        await asyncio.sleep(5)
        await client.disconnect()

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
import base64
from typing import Any, Callable, Optional
from dataclasses import dataclass, field

import websockets
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger("wsocket")


# ─── Types ──────────────────────────────────────────────────

@dataclass
class MessageMeta:
    id: str
    channel: str
    timestamp: float


@dataclass
class PresenceMember:
    client_id: str
    data: dict[str, Any] | None = None
    joined_at: float = 0.0


@dataclass
class HistoryMessage:
    id: str
    channel: str
    data: Any
    publisher_id: str
    timestamp: float
    sequence: int = 0


@dataclass
class HistoryResult:
    channel: str
    messages: list[HistoryMessage]
    has_more: bool = False


@dataclass
class WSocketOptions:
    """Configuration options for the wSocket client."""
    auto_reconnect: bool = True
    max_reconnect_attempts: int = 10
    reconnect_delay: float = 1.0
    token: str | None = None
    recover: bool = True


# ─── Presence ────────────────────────────────────────────────

class Presence:
    """Presence API for a channel."""

    def __init__(self, channel_name: str, send_fn: Callable):
        self._channel = channel_name
        self._send = send_fn
        self._enter_cbs: list[Callable] = []
        self._leave_cbs: list[Callable] = []
        self._update_cbs: list[Callable] = []
        self._members_cbs: list[Callable] = []

    def enter(self, data: dict[str, Any] | None = None) -> "Presence":
        """Enter the presence set with optional data."""
        self._send({"action": "presence.enter", "channel": self._channel, "data": data})
        return self

    def leave(self) -> "Presence":
        """Leave the presence set."""
        self._send({"action": "presence.leave", "channel": self._channel})
        return self

    def update(self, data: dict[str, Any]) -> "Presence":
        """Update presence data."""
        self._send({"action": "presence.update", "channel": self._channel, "data": data})
        return self

    def get(self) -> "Presence":
        """Request current members list."""
        self._send({"action": "presence.get", "channel": self._channel})
        return self

    def on_enter(self, cb: Callable) -> "Presence":
        self._enter_cbs.append(cb)
        return self

    def on_leave(self, cb: Callable) -> "Presence":
        self._leave_cbs.append(cb)
        return self

    def on_update(self, cb: Callable) -> "Presence":
        self._update_cbs.append(cb)
        return self

    def on_members(self, cb: Callable) -> "Presence":
        self._members_cbs.append(cb)
        return self

    def _emit_enter(self, member: PresenceMember) -> None:
        for cb in self._enter_cbs:
            try:
                cb(member)
            except Exception:
                pass

    def _emit_leave(self, member: PresenceMember) -> None:
        for cb in self._leave_cbs:
            try:
                cb(member)
            except Exception:
                pass

    def _emit_update(self, member: PresenceMember) -> None:
        for cb in self._update_cbs:
            try:
                cb(member)
            except Exception:
                pass

    def _emit_members(self, members: list[PresenceMember]) -> None:
        for cb in self._members_cbs:
            try:
                cb(members)
            except Exception:
                pass


# ─── Channel ────────────────────────────────────────────────

class Channel:
    """Represents a pub/sub channel."""

    def __init__(self, name: str, send_fn: Callable):
        self.name = name
        self._send = send_fn
        self._subscribed = False
        self._message_cbs: list[Callable] = []
        self._history_cbs: list[Callable] = []
        self.presence = Presence(name, send_fn)

    def subscribe(self, rewind: int | None = None) -> "Channel":
        """Subscribe to messages on this channel."""
        if not self._subscribed:
            msg: dict = {"action": "subscribe", "channel": self.name}
            if rewind:
                msg["rewind"] = rewind
            self._send(msg)
            self._subscribed = True
        return self

    def unsubscribe(self) -> None:
        """Unsubscribe from this channel."""
        self._send({"action": "unsubscribe", "channel": self.name})
        self._subscribed = False
        self._message_cbs.clear()

    def publish(self, data: Any, persist: bool = True) -> "Channel":
        """Publish data to this channel."""
        msg: dict = {
            "action": "publish",
            "channel": self.name,
            "data": data,
            "id": str(uuid.uuid4()),
        }
        if not persist:
            msg["persist"] = False
        self._send(msg)
        return self

    def history(
        self,
        limit: int | None = None,
        before: float | None = None,
        after: float | None = None,
        direction: str | None = None,
    ) -> "Channel":
        """Query message history for this channel."""
        msg: dict = {"action": "history", "channel": self.name}
        if limit:
            msg["limit"] = limit
        if before:
            msg["before"] = before
        if after:
            msg["after"] = after
        if direction:
            msg["direction"] = direction
        self._send(msg)
        return self

    def on_message(self, cb: Callable) -> Callable:
        """Register a message callback. Can be used as a decorator."""
        self._message_cbs.append(cb)
        return cb

    def on_history(self, cb: Callable) -> Callable:
        """Register a history result callback. Can be used as a decorator."""
        self._history_cbs.append(cb)
        return cb

    @property
    def is_subscribed(self) -> bool:
        return self._subscribed

    @property
    def has_listeners(self) -> bool:
        return len(self._message_cbs) > 0

    def _emit(self, data: Any, meta: MessageMeta) -> None:
        for cb in self._message_cbs:
            try:
                cb(data, meta)
            except Exception:
                pass

    def _emit_history(self, result: HistoryResult) -> None:
        for cb in self._history_cbs:
            try:
                cb(result)
            except Exception:
                pass

    def _mark_for_resubscribe(self) -> None:
        self._subscribed = False


# ─── PubSub Namespace ───────────────────────────────────────

class PubSubNamespace:
    """Scoped API for pub/sub operations.

    Usage: client.pubsub.channel('chat').subscribe()
    """

    def __init__(self, channel_fn: Callable[[str], Channel]):
        self._channel_fn = channel_fn

    def channel(self, name: str) -> Channel:
        """Get or create a channel (same as client.channel())."""
        return self._channel_fn(name)


# ─── wSocket Client ─────────────────────────────────────────

class WSocket:
    """Async WebSocket Pub/Sub client for wSocket."""

    def __init__(self, url: str, api_key: str, options: WSocketOptions | None = None):
        self.url = url
        self.api_key = api_key
        self.options = options or WSocketOptions()
        self._ws: Any = None
        self._state = "disconnected"
        self._channels: dict[str, Channel] = {}
        self._event_cbs: dict[str, list[Callable]] = {}
        self._reconnect_attempts = 0
        self._reconnect_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._last_message_ts: float = 0
        self._resume_token: str | None = None

        # Namespaces
        self.pubsub = PubSubNamespace(self.channel)
        self.push: PushClient | None = None

    # ─── Connection ─────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the wSocket server."""
        if self._state == "connected":
            return

        self._set_state("connecting")

        if self.options.token:
            ws_url = f"{self.url}?token={self.options.token}"
        else:
            ws_url = f"{self.url}?key={self.api_key}"

        try:
            self._ws = await ws_connect(ws_url)
            self._set_state("connected")
            self._reconnect_attempts = 0
            self._recv_task = asyncio.create_task(self._receive_loop())
            self._ping_task = asyncio.create_task(self._ping_loop())
            self._resubscribe_all()
        except Exception as e:
            self._set_state("disconnected")
            raise e

    async def disconnect(self) -> None:
        """Disconnect from the server."""
        self._set_state("disconnected")
        if self._ping_task:
            self._ping_task.cancel()
            self._ping_task = None
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    # ─── Channels ───────────────────────────────────────────

    def channel(self, name: str) -> Channel:
        """Get or create a channel.

        Deprecated: Use client.pubsub.channel(name) for new code.
        """
        if name not in self._channels:
            self._channels[name] = Channel(name, self._send)
        return self._channels[name]

    def configure_push(self, base_url: str, token: str, app_id: str) -> "PushClient":
        """Configure push notification access.

        Example:
            push = client.configure_push("http://localhost:9001", "api-key", "app-id")
            await push.send_to_member("user-1", title="Hello", body="World")
        """
        self.push = PushClient(base_url, token, app_id)
        return self.push

    # ─── Events ─────────────────────────────────────────────

    def on(self, event: str, callback: Callable) -> "WSocket":
        """Listen for client events: 'connected', 'disconnected', 'error', 'state'."""
        if event not in self._event_cbs:
            self._event_cbs[event] = []
        self._event_cbs[event].append(callback)
        return self

    @property
    def connection_state(self) -> str:
        return self._state

    # ─── Internal ───────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        if self._ws:
            try:
                asyncio.get_event_loop().create_task(self._ws.send(json.dumps(msg)))
            except RuntimeError:
                pass

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                self._handle_message(raw)
        except websockets.ConnectionClosed:
            self._emit("disconnected")
            if self._state != "disconnected" and self.options.auto_reconnect:
                self._schedule_reconnect()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._emit("error", e)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        action = msg.get("action")

        if action == "message":
            ch_name = msg.get("channel")
            if ch_name and ch_name in self._channels:
                ts = msg.get("timestamp", time.time() * 1000)
                if ts > self._last_message_ts:
                    self._last_message_ts = ts
                meta = MessageMeta(
                    id=msg.get("id", ""),
                    channel=ch_name,
                    timestamp=ts,
                )
                self._channels[ch_name]._emit(msg.get("data"), meta)

        elif action == "subscribed":
            self._emit("subscribed", msg.get("channel"))

        elif action == "unsubscribed":
            self._emit("unsubscribed", msg.get("channel"))

        elif action == "ack":
            if msg.get("id") == "resume" and isinstance(msg.get("data"), dict):
                self._resume_token = msg["data"].get("resumeToken")
            self._emit("ack", msg.get("id"), msg.get("channel"))

        elif action == "error":
            self._emit("error", Exception(msg.get("error", "Unknown error")))

        elif action == "pong":
            pass

        elif action == "presence.enter":
            ch = self._channels.get(msg.get("channel", ""))
            if ch:
                member = self._parse_member(msg.get("data", {}))
                ch.presence._emit_enter(member)

        elif action == "presence.leave":
            ch = self._channels.get(msg.get("channel", ""))
            if ch:
                member = self._parse_member(msg.get("data", {}))
                ch.presence._emit_leave(member)

        elif action == "presence.update":
            ch = self._channels.get(msg.get("channel", ""))
            if ch:
                member = self._parse_member(msg.get("data", {}))
                ch.presence._emit_update(member)

        elif action == "presence.members":
            ch = self._channels.get(msg.get("channel", ""))
            if ch and isinstance(msg.get("data"), list):
                members = [self._parse_member(m) for m in msg["data"]]
                ch.presence._emit_members(members)

        elif action == "history":
            ch = self._channels.get(msg.get("channel", ""))
            if ch and isinstance(msg.get("data"), dict):
                result = self._parse_history(msg["data"])
                ch._emit_history(result)

    @staticmethod
    def _parse_member(data: dict) -> PresenceMember:
        return PresenceMember(
            client_id=data.get("clientId", ""),
            data=data.get("data"),
            joined_at=data.get("joinedAt", 0),
        )

    @staticmethod
    def _parse_history(data: dict) -> HistoryResult:
        messages = []
        for m in data.get("messages", []):
            messages.append(HistoryMessage(
                id=m.get("id", ""),
                channel=m.get("channel", ""),
                data=m.get("data"),
                publisher_id=m.get("publisherId", ""),
                timestamp=m.get("timestamp", 0),
                sequence=m.get("sequence", 0),
            ))
        return HistoryResult(
            channel=data.get("channel", ""),
            messages=messages,
            has_more=data.get("hasMore", False),
        )

    def _set_state(self, state: str) -> None:
        prev = self._state
        self._state = state
        if prev != state:
            self._emit("state", state, prev)
            if state == "connected":
                self._emit("connected")

    def _emit(self, event: str, *args: Any) -> None:
        cbs = self._event_cbs.get(event, [])
        for cb in cbs:
            try:
                cb(*args)
            except Exception:
                pass

    def _resubscribe_all(self) -> None:
        # Connection state recovery: use resume
        if self.options.recover and self._last_message_ts > 0:
            channel_names = [
                ch.name for ch in self._channels.values() if ch.has_listeners
            ]
            if channel_names:
                token_data = json.dumps({
                    "channels": channel_names,
                    "lastTs": self._last_message_ts,
                })
                token = self._resume_token or base64.urlsafe_b64encode(
                    token_data.encode()
                ).decode().rstrip("=")
                self._send({"action": "resume", "resumeToken": token})
                for ch in self._channels.values():
                    if ch.has_listeners:
                        ch._mark_for_resubscribe()
                return

        # Fallback: simple resubscribe
        for ch in self._channels.values():
            if ch.has_listeners:
                ch._mark_for_resubscribe()
                ch.subscribe()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_attempts >= self.options.max_reconnect_attempts:
            self._set_state("disconnected")
            self._emit("error", Exception("Max reconnect attempts reached"))
            return

        self._set_state("reconnecting")
        delay = self.options.reconnect_delay * (2 ** self._reconnect_attempts)
        self._reconnect_attempts += 1

        async def _reconnect():
            await asyncio.sleep(delay)
            try:
                await self.connect()
            except Exception:
                pass  # will retry via close handler

        self._reconnect_task = asyncio.create_task(_reconnect())

    async def _ping_loop(self) -> None:
        try:
            while self._state == "connected":
                await asyncio.sleep(30)
                self._send({"action": "ping"})
        except asyncio.CancelledError:
            pass


# ─── Factory ────────────────────────────────────────────────

def create_client(
    url: str,
    api_key: str,
    options: WSocketOptions | None = None,
) -> WSocket:
    """
    Create a new wSocket client.

    Example:
        client = create_client("ws://localhost:9001", "your-api-key")
        await client.connect()

        chat = client.channel("chat")

        @chat.on_message
        def handle(data, meta):
            print(f"Received: {data}")

        chat.subscribe()
        chat.publish({"text": "Hello, world!"})
    """
    return WSocket(url, api_key, options)


# ─── Push Notifications ─────────────────────────────────────

class PushClient:
    """REST-based push notification client for wSocket.

    Supports registering device tokens (FCM/APNs) and sending push notifications.

    Example:
        push = PushClient("http://localhost:9001", "admin-jwt-token", "app-id")
        await push.register_fcm("device-token-123", member_id="user1")
        await push.send_to_member("user1", title="Hello", body="World")
    """

    def __init__(self, base_url: str, token: str, app_id: str):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._app_id = app_id

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        import aiohttp

        url = f"{self._base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, json=body) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise Exception(f"Push API error {resp.status}: {text}")
                return await resp.json()

    async def get_vapid_key(self) -> str | None:
        """Get the VAPID public key for Web Push subscription."""
        res = await self._api("GET", f"/api/admin/apps/{self._app_id}/push/config")
        return res.get("vapidPublicKey")

    async def register_fcm(self, device_token: str, member_id: str | None = None) -> str:
        """Register an FCM device token (Android)."""
        res = await self._api("POST", f"/api/admin/apps/{self._app_id}/push/register", {
            "platform": "fcm",
            "memberId": member_id,
            "deviceToken": device_token,
        })
        return res["subscriptionId"]

    async def register_apns(self, device_token: str, member_id: str | None = None) -> str:
        """Register an APNs device token (iOS)."""
        res = await self._api("POST", f"/api/admin/apps/{self._app_id}/push/register", {
            "platform": "apns",
            "memberId": member_id,
            "deviceToken": device_token,
        })
        return res["subscriptionId"]

    async def register_web_push(
        self,
        endpoint: str,
        keys: dict,
        member_id: str | None = None,
    ) -> str:
        """Register a Web Push subscription."""
        res = await self._api("POST", f"/api/admin/apps/{self._app_id}/push/register", {
            "platform": "web",
            "memberId": member_id,
            "webPush": {"endpoint": endpoint, "keys": keys},
        })
        return res["subscriptionId"]

    async def unregister(self, member_id: str, platform: str | None = None) -> int:
        """Unregister push subscriptions for a member."""
        res = await self._api("DELETE", f"/api/admin/apps/{self._app_id}/push/unregister", {
            "memberId": member_id,
            "platform": platform,
        })
        return res["removed"]

    async def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a specific push subscription by its ID."""
        res = await self._api("DELETE", f"/api/admin/apps/{self._app_id}/push/subscriptions/{subscription_id}")
        return res.get("deleted", False)

    async def send_to_member(self, member_id: str, **payload) -> dict:
        """Send a push notification to a specific member."""
        return await self._api("POST", f"/api/admin/apps/{self._app_id}/push/send", {
            "memberId": member_id,
            **payload,
        })

    async def send_to_members(self, member_ids: list[str], **payload) -> dict:
        """Send a push notification to multiple members."""
        return await self._api("POST", f"/api/admin/apps/{self._app_id}/push/send", {
            "memberIds": member_ids,
            **payload,
        })

    async def broadcast(self, **payload) -> dict:
        """Broadcast a push notification to all subscribers."""
        return await self._api("POST", f"/api/admin/apps/{self._app_id}/push/send", {
            "broadcast": True,
            **payload,
        })

    async def get_stats(self) -> dict:
        """Get push notification statistics."""
        res = await self._api("GET", f"/api/admin/apps/{self._app_id}/push/stats")
        return res["stats"]

