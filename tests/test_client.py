"""Tests for the wSocket Python SDK."""

import json
import base64
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from wsocket.client import (
    WSocket,
    WSocketOptions,
    Channel,
    Presence,
    MessageMeta,
    PresenceMember,
    HistoryMessage,
    HistoryResult,
    create_client,
)


class TestCreateClient:
    def test_returns_wsocket_instance(self):
        client = create_client("ws://localhost:9001", "my-key")
        assert isinstance(client, WSocket)
        assert client.url == "ws://localhost:9001"
        assert client.api_key == "my-key"

    def test_default_options(self):
        client = create_client("ws://localhost:9001", "k")
        assert client.options.auto_reconnect is True
        assert client.options.max_reconnect_attempts == 10
        assert client.options.recover is True

    def test_custom_options(self):
        opts = WSocketOptions(auto_reconnect=False, recover=False, max_reconnect_attempts=3)
        client = create_client("ws://localhost:9001", "k", opts)
        assert client.options.auto_reconnect is False
        assert client.options.recover is False
        assert client.options.max_reconnect_attempts == 3


class TestChannel:
    def setup_method(self):
        self.sent: list = []
        self.channel = Channel("chat", lambda msg: self.sent.append(msg))

    def test_subscribe_sends_action(self):
        self.channel.subscribe()
        assert len(self.sent) == 1
        assert self.sent[0]["action"] == "subscribe"
        assert self.sent[0]["channel"] == "chat"

    def test_subscribe_idempotent(self):
        self.channel.subscribe()
        self.channel.subscribe()
        assert len(self.sent) == 1

    def test_subscribe_with_rewind(self):
        self.channel.subscribe(rewind=10)
        assert self.sent[0]["rewind"] == 10

    def test_unsubscribe(self):
        self.channel.subscribe()
        self.channel.unsubscribe()
        assert self.sent[-1]["action"] == "unsubscribe"
        assert not self.channel.is_subscribed

    def test_publish(self):
        self.channel.publish({"text": "hello"})
        msg = self.sent[0]
        assert msg["action"] == "publish"
        assert msg["channel"] == "chat"
        assert msg["data"] == {"text": "hello"}
        assert "id" in msg

    def test_publish_ephemeral(self):
        self.channel.publish({"text": "hi"}, persist=False)
        assert self.sent[0]["persist"] is False

    def test_on_message_decorator(self):
        received = []

        @self.channel.on_message
        def handler(data, meta):
            received.append(data)

        assert self.channel.has_listeners is True
        self.channel._emit("test-data", MessageMeta(id="1", channel="chat", timestamp=0))
        assert received == ["test-data"]

    def test_history_query(self):
        self.channel.history(limit=50, direction="backward")
        msg = self.sent[0]
        assert msg["action"] == "history"
        assert msg["limit"] == 50
        assert msg["direction"] == "backward"


class TestPresence:
    def setup_method(self):
        self.sent: list = []
        self.presence = Presence("room", lambda msg: self.sent.append(msg))

    def test_enter(self):
        self.presence.enter({"name": "Alice"})
        assert self.sent[0]["action"] == "presence.enter"
        assert self.sent[0]["data"] == {"name": "Alice"}

    def test_leave(self):
        self.presence.leave()
        assert self.sent[0]["action"] == "presence.leave"

    def test_update(self):
        self.presence.update({"status": "away"})
        assert self.sent[0]["action"] == "presence.update"

    def test_get(self):
        self.presence.get()
        assert self.sent[0]["action"] == "presence.get"

    def test_callbacks(self):
        entered = []
        left = []

        self.presence.on_enter(lambda m: entered.append(m))
        self.presence.on_leave(lambda m: left.append(m))

        member = PresenceMember(client_id="c1", data={"name": "Bob"})
        self.presence._emit_enter(member)
        self.presence._emit_leave(member)

        assert len(entered) == 1
        assert entered[0].client_id == "c1"
        assert len(left) == 1


class TestWSocketMessageHandling:
    def setup_method(self):
        self.client = create_client("ws://localhost:9001", "key")

    def test_handle_message_dispatches_to_channel(self):
        ch = self.client.channel("chat")
        received = []

        @ch.on_message
        def handler(data, meta):
            received.append(data)

        self.client._handle_message(json.dumps({
            "action": "message",
            "channel": "chat",
            "data": {"text": "hello"},
            "id": "msg-1",
            "timestamp": 1000,
        }))

        assert received == [{"text": "hello"}]

    def test_handle_message_unknown_channel_ignored(self):
        self.client._handle_message(json.dumps({
            "action": "message",
            "channel": "unknown",
            "data": "test",
        }))
        # No exception

    def test_handle_presence_enter(self):
        ch = self.client.channel("room")
        entered = []
        ch.presence.on_enter(lambda m: entered.append(m))

        self.client._handle_message(json.dumps({
            "action": "presence.enter",
            "channel": "room",
            "data": {"clientId": "c1", "data": {"name": "Alice"}},
        }))

        assert len(entered) == 1
        assert entered[0].client_id == "c1"

    def test_handle_ack_with_resume_token(self):
        self.client._handle_message(json.dumps({
            "action": "ack",
            "id": "resume",
            "data": {"resumeToken": "new-token-123"},
        }))
        assert self.client._resume_token == "new-token-123"

    def test_handle_error(self):
        errors = []
        self.client.on("error", lambda e: errors.append(str(e)))

        self.client._handle_message(json.dumps({
            "action": "error",
            "error": "Rate limit exceeded",
        }))

        assert len(errors) == 1
        assert "Rate limit" in errors[0]

    def test_invalid_json_ignored(self):
        self.client._handle_message("not-json{{{")
        # No exception

    def test_connection_state(self):
        assert self.client.connection_state == "disconnected"

    def test_state_events(self):
        states = []
        self.client.on("state", lambda new, old: states.append((new, old)))
        self.client._set_state("connecting")
        self.client._set_state("connected")
        assert states == [("connecting", "disconnected"), ("connected", "connecting")]


class TestDataClasses:
    def test_message_meta(self):
        m = MessageMeta(id="1", channel="ch", timestamp=1234.5)
        assert m.id == "1"
        assert m.channel == "ch"
        assert m.timestamp == 1234.5

    def test_presence_member(self):
        m = PresenceMember(client_id="c1")
        assert m.data is None
        assert m.joined_at == 0.0

    def test_history_message(self):
        h = HistoryMessage(id="h1", channel="ch", data="test", publisher_id="p1", timestamp=100)
        assert h.sequence == 0

    def test_history_result(self):
        r = HistoryResult(channel="ch", messages=[], has_more=True)
        assert r.has_more is True

    def test_parse_member(self):
        m = WSocket._parse_member({"clientId": "c1", "data": {"name": "Bob"}, "joinedAt": 100})
        assert m.client_id == "c1"
        assert m.data == {"name": "Bob"}
        assert m.joined_at == 100

    def test_parse_history(self):
        r = WSocket._parse_history({
            "channel": "chat",
            "messages": [
                {"id": "m1", "channel": "chat", "data": "hello", "publisherId": "p1", "timestamp": 1000, "sequence": 1},
            ],
            "hasMore": False,
        })
        assert r.channel == "chat"
        assert len(r.messages) == 1
        assert r.messages[0].publisher_id == "p1"
