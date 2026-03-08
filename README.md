# wSocket Python SDK

Official Python SDK for [wSocket](https://wsocket.io) — Realtime Pub/Sub over WebSockets.

[![PyPI](https://img.shields.io/pypi/v/wsocket-io)](https://pypi.org/project/wsocket-io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Installation

```bash
pip install wsocket-io
```

## Quick Start

```python
import asyncio
from wsocket import create_client

async def main():
    client = create_client("wss://node00.wsocket.online", "your-api-key")
    await client.connect()

    chat = client.channel("chat:general")

    @chat.on_message
    def handle(data, meta):
        print(f"[{meta.channel}] {data}")

    chat.subscribe()
    chat.publish({"text": "Hello from Python!"})

    await asyncio.sleep(5)
    await client.disconnect()

asyncio.run(main())
```

## Features

- **Pub/Sub** — Subscribe and publish to channels in real-time
- **Presence** — Track who is online in a channel
- **History** — Retrieve past messages
- **Connection Recovery** — Automatic reconnection with message replay
- **Async/Await** — Built on `asyncio` and `websockets`

## Presence

```python
chat = client.channel("chat:general")

@chat.presence.on_enter
def user_joined(member):
    print(f"Joined: {member.client_id}")

@chat.presence.on_leave
def user_left(member):
    print(f"Left: {member.client_id}")

chat.presence.enter({"name": "Alice"})
members = chat.presence.get()
```

## History

```python
@chat.on_history
def handle_history(result):
    for msg in result.messages:
        print(f"[{msg['timestamp']}] {msg['data']}")

chat.history(limit=50)
```

## Push Notifications

```python
from wsocket_io import PushClient

push = PushClient('https://your-server.com', 'your-api-key', 'your-app-id')

# Register device
push.register_fcm('device-token', 'user-123')

# Send to member
push.send_to_member('user-123', {'title': 'Hello', 'body': 'World'})

# Broadcast
push.broadcast({'title': 'News', 'body': 'Breaking news!'})

# Channel targeting
push.add_channel('subscription-id', 'alerts')
push.remove_channel('subscription-id', 'alerts')

# VAPID key
vapid_key = push.get_vapid_key()

# List subscriptions
subs = push.list_subscriptions('user-123')

# Delete subscription
push.delete_subscription('subscription-id')
```

## Requirements

- Python >= 3.9
- `websockets >= 12.0`

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
