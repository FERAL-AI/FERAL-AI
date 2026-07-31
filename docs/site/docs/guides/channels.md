---
id: channels
title: Channels
sidebar_position: 7
slug: /guides/channels
---

# Channels

FERAL connects to external messaging platforms through **channels**. Each channel bridges a platform's API into the Brain's session system, so users can talk to the agent from Telegram, Slack, Discord, or receive push notifications — all with the same capabilities as the web UI.

## ChannelManager Architecture

The `ChannelManager` is a singleton that registers, starts, and supervises channel adapters. Each adapter runs in its own asyncio task and translates platform-specific messages into `FeralMessage` objects.

```python
from feral_core.channels import ChannelManager

manager = ChannelManager(brain=brain)
manager.register("telegram", TelegramChannel(token="..."))
manager.register("slack", SlackChannel(token="..."))
manager.register("discord", DiscordChannel(token="..."))
await manager.start_all()
```

Each channel adapter implements a common interface:

```python
class ChannelAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, user_id: str, message: FeralMessage) -> None: ...
    async def on_message(self, callback: Callable) -> None: ...
```

## Telegram Bot

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) and get the token.
2. Store the token in the vault:

```bash
feral setup --from-step channels   # prompts for the token, probes getMe
```

3. Enable the channel in config:

```json
// ~/.feral/settings.json — "channels" section
{
  "channels": {
    "telegram": {
      "enabled": true,
      "allowed_users": [12345678],
      "features": {
        "voice_messages": true,
        "inline_mode": true
      }
    }
  }
}
```

4. Start FERAL — the Telegram channel connects automatically.

### Features

| Feature | Status |
|:--------|:-------|
| Text messages | Supported |
| Voice messages (transcribed via Whisper) | Supported |
| Photos (analyzed via vision) | Supported |
| Inline mode (use FERAL in any chat) | Supported |
| Group chats | Supported (mention-triggered) |
| SDUI cards rendered as Telegram messages | Supported |

## Slack Integration

### Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps).
2. Enable **Socket Mode** and add bot scopes: `chat:write`, `app_mentions:read`, `im:history`.
3. Store tokens:

```bash
feral setup --from-step channels   # prompts for both tokens, probes auth.test
```

4. Configure:

```yaml
channels:
  slack:
    enabled: true
    socket_mode: true
    respond_to:
      - direct_messages
      - app_mentions
    default_channel: "#general"
```

### Thread Support

FERAL maintains session continuity per Slack thread. A new thread starts a new session; replies within a thread continue the same conversation context.

## Discord Gateway

### Setup

1. Create an application at [discord.com/developers](https://discord.com/developers/applications).
2. Add a bot, enable **Message Content Intent**.
3. Invite the bot to your server with `Send Messages` + `Read Message History` permissions.

```bash
feral setup --from-step channels   # prompts for the token, probes users/@me
```

```yaml
channels:
  discord:
    enabled: true
    command_prefix: "!"
    respond_in_channels:
      - "feral-chat"
      - "general"
    respond_to_dms: true
```

### Slash Commands

FERAL auto-registers Discord slash commands on startup:

| Command | Description |
|:--------|:------------|
| `/ask <question>` | Ask the agent a question |
| `/remember <fact>` | Store a fact in memory |
| `/status` | Show agent status and uptime |
| `/voice` | Join voice channel (experimental) |

## Push Notifications

FERAL sends push notifications via **Firebase Cloud Messaging** (Android/web) and **Apple Push Notification service** (iOS) for proactive alerts, reminders, and task completions.

### Configuration

```yaml
channels:
  push:
    enabled: true
    fcm:
      credentials_file: ~/.feral/firebase-credentials.json
    apns:
      key_file: ~/.feral/apns-auth-key.p8
      key_id: "ABC123"
      team_id: "DEF456"
      bundle_id: "io.feral.app"
      environment: production  # or sandbox
```

### Sending Notifications

Push notifications are triggered by the Brain when proactive events fire (health alerts, reminders, task completions):

```python
from feral_core.channels import PushChannel

push = PushChannel(config)

await push.send(
    user_id="user_123",
    message=FeralMessage(
        type="push_notification",
        payload={
            "title": "Hydration Reminder",
            "body": "You haven't logged water in 3 hours.",
            "action_url": "/dashboard",
            "priority": "normal",
        },
    ),
)
```

### Device Registration

Clients register their push tokens via the Brain API:

```bash
curl -X POST http://localhost:9090/api/push/register \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_123", "platform": "fcm", "token": "eKx7..."}'
```

## Channel-Specific Formatting

Each adapter translates SDUI payloads into platform-native formatting:

| SDUI Component | Telegram | Slack | Discord |
|:---------------|:---------|:------|:--------|
| `MetricCard` | Bold text + emoji | Block Kit section | Embed field |
| `DataTable` | Monospace text | Block Kit table | Code block |
| `FormCard` | Inline keyboard | Modal | Button row |
| `ImageCard` | Photo message | Image block | Embed image |
| `MarkdownCard` | Markdown (subset) | mrkdwn | Markdown |
