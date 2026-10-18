"""Final Discord payload guard and durable, silent release edits.

Both Discord REST transports are guarded, including interaction webhooks. Raw
drafts contain presentation markers, not webhook URLs/tokens. Public messages
are edited through the bot-token channel route, so interaction expiry/restarts
do not prevent release. Ephemeral replies are deliberately never retained.
"""

import asyncio
from contextlib import closing
from copy import deepcopy
import json
import logging
import sqlite3
from datetime import timedelta

import discord
from discord.http import Route
from discord.webhook.async_ import AsyncWebhookAdapter

from services.spoilers import current_policy, render_text


logger = logging.getLogger(__name__)
MESSAGE_KEYS = {"content", "embeds", "components", "flags", "attachments"}


def render_payload(draft):
    """Visit human-readable fields only; IDs, links and mentions stay intact."""
    from services.team_emojis import team_text
    result = deepcopy(draft)

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = dict(value)
        # Native embed titles/field names cannot reliably render spoilers.
        # Put identifying text in the Markdown-capable body instead.
        if "name" in value and "value" in value and "⟦team:" in team_text(value["name"]):
            value["value"] = value["name"] + "\n" + value["value"]
            value["name"] = "\xa0"
        if "title" in value and "⟦team:" in team_text(value["title"]):
            value["description"] = "## " + value.pop("title") + "\n\n" + value.get("description", "")
        original_label = value.get("label", "")
        hidden_label = "⟦team:" in original_label and render_text(original_label, neutral=True).strip("\xa0 ✓") == "?"
        value = {key: item if key in {"emoji", "allowed_mentions", "default_values"} else walk(item)
                 for key, item in value.items()}
        for key in ("content", "title", "description", "name", "value", "label", "placeholder", "text"):
            if isinstance(value.get(key), str):
                if key == "value" and "label" in value:
                    continue  # Select option values are operational IDs.
                neutral = key in {"label", "placeholder"} or "value" in value and "label" in value
                value[key] = render_text(team_text(value[key]), neutral=neutral).replace("\u200b", "\xa0")
        # Labels/menus cannot render Markdown. Remove an identity-bearing emoji
        # whenever its accompanying label has become a neutral spoiler label.
        if hidden_label:
            from services.team_emojis import custom_emoji
            placeholder = custom_emoji("beeQuestion")
            value["emoji"] = ({"id": str(placeholder.id), "name": placeholder.name,
                               "animated": getattr(placeholder, "animated", False)} if placeholder else {"name": "❓"})
        return value

    result = walk(result)
    # External previews may expose scores, winners or bracket logos.
    if not result.get("embeds"):
        result["flags"] = result.get("flags", 0) | 4
    return result


class SpoilerDelivery:
    def __init__(self, client, connect):
        self.client, self.connect = client, connect
        self.original_request = client.http.request
        self._lock = asyncio.Lock()
        self._pending = []
        with closing(connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS spoiler_messages (
                channel_id INTEGER, message_id INTEGER, draft TEXT, rendered TEXT,
                PRIMARY KEY(channel_id, message_id))""")
            db.execute("CREATE TABLE IF NOT EXISTS spoiler_migrations (name TEXT PRIMARY KEY)")

    def remember(self, response, draft, rendered):
        if not isinstance(response, dict) or draft.get("flags", 0) & 64:
            return
        resource = response.get("resource")
        message = resource.get("message", response) if isinstance(resource, dict) else response
        if not isinstance(message, dict) or not message.get("id") or not message.get("channel_id"):
            return
        if message.get("flags", 0) & 64:
            return
        raw = {key: value for key, value in draft.items() if key in MESSAGE_KEYS}
        # No file uploads currently exist. Attachment IDs are never duplicated
        # or re-uploaded by a release edit.
        raw.pop("attachments", None)
        with closing(self.connect()) as db, db:
            existing = db.execute("SELECT draft, rendered FROM spoiler_messages WHERE channel_id=? AND message_id=?",
                                  (int(message["channel_id"]), int(message["id"]))).fetchone()
            previous_draft, previous_render = (json.loads(value) for value in existing) if existing else ({}, {})
            raw = {**previous_draft, **raw}
            if "⟦" not in json.dumps(raw, ensure_ascii=False):
                return
            rendered = {**previous_render, **rendered}
            db.execute("INSERT OR REPLACE INTO spoiler_messages VALUES (?, ?, ?, ?)",
                       (int(message["channel_id"]), int(message["id"]), json.dumps(raw),
                        json.dumps({key: value for key, value in rendered.items() if key in raw}, sort_keys=True)))

    def remember_safely(self, response, draft, rendered):
        try:
            self.remember(response, draft, rendered)
        except (sqlite3.Error, OSError):
            # A failed tracking write must never turn an acknowledged send
            # into a duplicate retry by the voting/reminder delivery code.
            self._pending.append((response, deepcopy(draft), deepcopy(rendered)))
            logger.exception("Spoiler tracking write failed; keeping draft for retry")

    async def release_once(self):
        async with self._lock:
            pending, self._pending = self._pending, []
            for args in pending:
                self.remember_safely(*args)
            with closing(self.connect()) as db:
                rows = db.execute("SELECT channel_id, message_id, draft, rendered FROM spoiler_messages").fetchall()
            for channel_id, message_id, raw, previous in rows:
                draft = json.loads(raw)
                payload = render_payload(draft)
                serialized = json.dumps({key: value for key, value in payload.items() if key in draft}, sort_keys=True)
                if serialized == previous:
                    continue
                payload["allowed_mentions"] = {"parse": [], "replied_user": False}
                try:
                    await self.original_request(Route("PATCH", "/channels/{channel_id}/messages/{message_id}",
                                                      channel_id=channel_id, message_id=message_id), json=payload)
                except discord.NotFound:
                    with closing(self.connect()) as db, db:
                        db.execute("DELETE FROM spoiler_messages WHERE channel_id=? AND message_id=?", (channel_id, message_id))
                except discord.HTTPException:
                    logger.warning("Could not refresh spoiler message %s; will retry", message_id, exc_info=True)
                else:
                    with closing(self.connect()) as db, db:
                        db.execute("UPDATE spoiler_messages SET rendered=? WHERE channel_id=? AND message_id=?",
                                   (serialized, channel_id, message_id))

    async def run(self):
        await self.client.wait_until_ready()
        try:
            await self.adopt_recent_messages()
        except Exception:
            # A denied/invalid old message must not disable release edits for
            # new messages. The unfinished migration retries on next startup.
            logger.exception("Old-message spoiler migration failed; new-message release remains active")
        while True:
            try:
                await self.release_once()
            except Exception:
                logger.exception("Spoiler release pass failed; will retry")
            await asyncio.sleep(15)

    async def adopt_recent_messages(self):
        """One-time migration of recent public messages from the old renderer."""
        from services.spoiler_legacy import legacy_payload
        policy = current_policy()
        with closing(self.connect()) as db:
            if db.execute("SELECT 1 FROM spoiler_migrations WHERE name='recent-v1'").fetchone():
                return
        if policy is None:
            return
        success = True
        for guild in self.client.guilds:
            for channel in [*guild.text_channels, *guild.threads]:
                try:
                    async for message in channel.history(limit=None, after=policy.clock() - timedelta(hours=24)):
                        if message.author.id != self.client.user.id:
                            continue
                        with closing(self.connect()) as db:
                            if db.execute("SELECT 1 FROM spoiler_messages WHERE channel_id=? AND message_id=?",
                                          (channel.id, message.id)).fetchone():
                                continue
                        raw = {"content": message.content or None,
                               "embeds": [embed.to_dict() for embed in message.embeds],
                               "components": [component.to_dict() for component in message.components],
                               "flags": message.flags.value}
                        draft = legacy_payload(raw)
                        rendered = render_payload(draft)
                        rendered["allowed_mentions"] = {"parse": [], "replied_user": False}
                        if rendered == {**raw, "allowed_mentions": rendered["allowed_mentions"]}:
                            continue
                        await self.original_request(Route("PATCH", "/channels/{channel_id}/messages/{message_id}",
                                                          channel_id=channel.id, message_id=message.id), json=rendered)
                        self.remember_safely({"channel_id": str(channel.id), "id": str(message.id)}, draft, rendered)
                except discord.HTTPException:
                    success = False
                    logger.warning("Could not migrate old spoiler messages in channel %s", channel.id)
        if success:
            with closing(self.connect()) as db, db:
                db.execute("INSERT OR IGNORE INTO spoiler_migrations VALUES ('recent-v1')")

    def uninstall(self):
        self.client.http.request = self.original_request
        if getattr(self, "original_webhook_request", None):
            AsyncWebhookAdapter.request = self.original_webhook_request

    def install(self):
        delivery = self

        async def guarded_request(route, **kwargs):
            draft = kwargs.get("json")
            is_message = route.method in {"POST", "PATCH"} and "/messages" in route.path
            if is_message and isinstance(draft, dict) and current_policy() is not None:
                rendered = render_payload(draft)
                kwargs["json"] = rendered
                response = await delivery.original_request(route, **kwargs)
                delivery.remember_safely(response, draft, rendered)
                return response
            return await delivery.original_request(route, **kwargs)

        self.client.http.request = guarded_request
        # InteractionResponse and Webhook bypass client.http entirely.
        original = AsyncWebhookAdapter.request
        self.original_webhook_request = original

        async def guarded_webhook(adapter, route, session, **kwargs):
            payload = kwargs.get("payload")
            draft = payload.get("data") if isinstance(payload, dict) and payload.get("type") in {4, 7} else payload
            is_message = isinstance(draft, dict) and any(key in draft for key in ("content", "embeds", "components"))
            if not is_message or current_policy() is None:
                return await original(adapter, route, session, **kwargs)
            rendered = render_payload(draft)
            kwargs["payload"] = {**payload, "data": rendered} if draft is not payload else rendered
            response = await original(adapter, route, session, **kwargs)
            delivery.remember_safely(response, draft, rendered)
            return response

        AsyncWebhookAdapter.request = guarded_webhook
