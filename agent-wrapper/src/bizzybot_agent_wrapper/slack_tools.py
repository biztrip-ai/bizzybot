"""In-process MCP server that lets the agent act on its own Slack workspace.

The agent-wrapper already holds the workspace's bot token (Central-Dispatch
hands it over at registration), so these tools run in this process against the
same AsyncWebClient that posts replies — no separate server, no extra auth. The
agent sees them as `mcp__bizzybot__<tool>`.

Each tool returns compact JSON. Slack API failures come back as tool errors
(`is_error`) instead of raising, and a `missing_scope` error says which scope is
missing, since workspaces installed before a scope was added keep the old
grant until the app is reinstalled.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .slack_io import upload_files

log = logging.getLogger("agent-wrapper.slack-tools")

SERVER_NAME = "bizzybot"

# Hard cap on how many rows a list tool returns, however many pages Slack has.
MAX_RESULTS = 1000
PAGE_SIZE = 200

TOOLS_PROMPT = """\
You have `mcp__bizzybot__*` tools for the Slack workspace you're talking in
(list_channels, list_users, create_channel, archive_channel, post_message,
read_messages, add_reaction). Use them for anything about this workspace. Other
Slack tools you may have can point at a different workspace. Only archive a
channel when the person asked for that specific channel to be archived.
Your reply to the current conversation is posted for you; use post_message
only to write somewhere else (another channel or thread). To notify a person
or agent, include their mention token `<@USERID>` (ids from list_users);
plain "@name" text notifies nobody."""

# Slack error codes from conversations.archive, explained so the agent can tell
# the person what to do instead of retrying.
_ARCHIVE_ERRORS = {
    "not_in_channel": "the bot isn't a member of that channel. Someone needs to "
    "add it first (/invite the bot in the channel), then try again.",
    "cant_archive_general": "the workspace's default channel can't be archived.",
    "cant_archive_required": "that channel is required by the workspace and can't be archived.",
    "restricted_action": "workspace settings don't let this bot archive channels.",
    "channel_not_found": "no such channel, or the bot can't see it (private channels "
    "are only visible once the bot is a member).",
}


def _ok(data: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _slack_error(method: str, e: SlackApiError) -> dict[str, Any]:
    data = e.response.data if isinstance(e.response.data, dict) else {}
    code = data.get("error", "unknown_error")
    if code == "missing_scope":
        return _err(
            f"Slack {method} failed: the bot is missing the `{data.get('needed', '?')}` "
            "scope. A workspace admin needs to grant it with Reinstall on the app's "
            "card in the Bizzybot dashboard, then restart the agent-wrapper."
        )
    return _err(f"Slack {method} failed: {code}")


def _guard(
    method: str,
) -> Callable[
    [Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]],
    Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
]:
    """Turn Slack and transport exceptions into tool errors. An exception
    escaping a handler would surface to the agent as an opaque MCP failure."""

    def wrap(fn):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await fn(args)
            except SlackApiError as e:
                return _slack_error(method, e)
            except Exception as e:  # noqa: BLE001
                log.exception("%s tool failed", method)
                return _err(f"Slack {method} failed: {e}")

        return handler

    return wrap


def _matches(query: str, *fields: Any) -> bool:
    q = query.lower()
    return any(isinstance(f, str) and q in f.lower() for f in fields)


def _limit(args: dict[str, Any]) -> int:
    try:
        n = int(args.get("limit") or MAX_RESULTS)
    except (TypeError, ValueError):
        n = MAX_RESULTS
    return max(1, min(n, MAX_RESULTS))


async def _paginate(call: Callable[..., Awaitable[Any]], key: str, **kwargs: Any):
    """Yield every item under `key` across Slack's cursor pages."""
    cursor = None
    while True:
        resp = await call(limit=PAGE_SIZE, cursor=cursor, **kwargs)
        for item in resp.get(key) or []:
            yield item
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def _channel_row(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "is_private": bool(c.get("is_private")),
        "is_archived": bool(c.get("is_archived")),
        "is_member": bool(c.get("is_member")),
        "num_members": c.get("num_members"),
        "topic": (c.get("topic") or {}).get("value") or "",
        "purpose": (c.get("purpose") or {}).get("value") or "",
    }


def _user_row(u: dict[str, Any]) -> dict[str, Any]:
    p = u.get("profile") or {}
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "real_name": u.get("real_name") or p.get("real_name") or "",
        "display_name": p.get("display_name") or "",
        "title": p.get("title") or "",
        "is_bot": bool(u.get("is_bot")) or u.get("id") == "USLACKBOT",
        "is_admin": bool(u.get("is_admin")),
        "tz": u.get("tz") or "",
    }


async def _resolve_channel(slack: AsyncWebClient, ref: str) -> Optional[dict[str, Any]]:
    """Look up a channel by id, or by name (with or without '#'). Names are
    matched exactly against every channel the bot can see, archived included."""
    ref = ref.strip()
    if re.fullmatch(r"[CG][A-Z0-9]{6,}", ref):
        try:
            return (await slack.conversations_info(channel=ref))["channel"]
        except SlackApiError as e:
            data = e.response.data if isinstance(e.response.data, dict) else {}
            if data.get("error") == "channel_not_found":
                return None
            raise
    name = ref.lstrip("#").lower()
    async for c in _paginate(
        slack.conversations_list,
        "channels",
        types="public_channel,private_channel",
        exclude_archived=False,
    ):
        if (c.get("name") or "").lower() == name:
            return c
    return None


# Longest message text read_messages returns per message; the rest is cut.
MAX_READ_TEXT = 4000


def _message_row(m: dict[str, Any]) -> dict[str, Any]:
    text = m.get("text") or ""
    row: dict[str, Any] = {
        "ts": m.get("ts"),
        "user": m.get("user") or (m.get("bot_profile") or {}).get("name"),
        "is_bot": bool(m.get("bot_id")),
        "text": text if len(text) <= MAX_READ_TEXT else text[:MAX_READ_TEXT] + " …[truncated]",
    }
    if m.get("thread_ts") and m.get("thread_ts") != m.get("ts"):
        row["thread_ts"] = m["thread_ts"]
    if m.get("reply_count"):
        row["reply_count"] = m["reply_count"]
    if m.get("files"):
        row["files"] = [f.get("name") for f in m["files"]]
    if m.get("reactions"):
        row["reactions"] = [r.get("name") for r in m["reactions"]]
    return row


def build_slack_mcp_server(slack: AsyncWebClient) -> McpSdkServerConfig:
    @tool(
        "list_channels",
        "List channels in this Slack workspace. Returns id, name, privacy, whether "
        "the bot is a member, member count, topic and purpose. Private channels "
        "are only listed if the bot is in them.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Case-insensitive substring to match against name, topic or purpose.",
                },
                "include_private": {
                    "type": "boolean",
                    "description": "Also list private channels the bot is in. Default true.",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived channels. Default false.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum rows to return (1-{MAX_RESULTS}).",
                },
            },
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    @_guard("conversations.list")
    async def list_channels(args: dict[str, Any]) -> dict[str, Any]:
        types = "public_channel"
        if args.get("include_private", True):
            types += ",private_channel"
        query = (args.get("query") or "").strip()
        limit = _limit(args)
        rows: list[dict[str, Any]] = []
        async for c in _paginate(
            slack.conversations_list,
            "channels",
            types=types,
            exclude_archived=not args.get("include_archived", False),
        ):
            row = _channel_row(c)
            if query and not _matches(query, row["name"], row["topic"], row["purpose"]):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return _ok({"count": len(rows), "channels": rows})

    @tool(
        "list_users",
        "List people in this Slack workspace. Returns id, handle, real and display "
        "name, title, bot/admin flags and timezone. Use the id to @-mention "
        "someone as <@ID>. Deactivated accounts are skipped.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Case-insensitive substring to match against handle, real name, display name or title.",
                },
                "include_bots": {
                    "type": "boolean",
                    "description": "Include bot and app users. Default false.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum rows to return (1-{MAX_RESULTS}).",
                },
            },
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    @_guard("users.list")
    async def list_users(args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        include_bots = bool(args.get("include_bots", False))
        limit = _limit(args)
        rows: list[dict[str, Any]] = []
        async for u in _paginate(slack.users_list, "members"):
            if u.get("deleted"):
                continue
            row = _user_row(u)
            if row["is_bot"] and not include_bots:
                continue
            if query and not _matches(
                query, row["name"], row["real_name"], row["display_name"], row["title"]
            ):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return _ok({"count": len(rows), "users": rows})

    @tool(
        "create_channel",
        "Create a new channel in this Slack workspace. The bot becomes a member, "
        "and in channels it created it receives every message, not just "
        "@-mentions. "
        "Optionally set its topic and purpose and invite people by user id "
        "(see list_users). Names must be lowercase, max 80 chars, with no spaces "
        "or periods.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Channel name, without the leading #."},
                "is_private": {
                    "type": "boolean",
                    "description": "Create a private channel. Default false.",
                },
                "topic": {"type": "string", "description": "Channel topic."},
                "purpose": {"type": "string", "description": "Channel purpose / description."},
                "invite_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User ids to add to the channel.",
                },
            },
            "required": ["name"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    @_guard("conversations.create")
    async def create_channel(args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip().lstrip("#")
        if not name:
            return _err("create_channel needs a channel name")
        resp = await slack.conversations_create(
            name=name, is_private=bool(args.get("is_private", False))
        )
        channel = resp["channel"]
        cid = channel["id"]
        # The channel exists from here on. A failed follow-up step is reported
        # alongside it rather than as a tool error, so the agent doesn't retry
        # the create and hit name_taken.
        warnings: list[str] = []

        async def step(label: str, call: Awaitable[Any]) -> bool:
            try:
                await call
                return True
            except SlackApiError as e:
                data = e.response.data if isinstance(e.response.data, dict) else {}
                warnings.append(f"{label} failed: {data.get('error', 'unknown_error')}")
                return False

        topic, purpose = args.get("topic"), args.get("purpose")
        if topic and await step(
            "setting topic", slack.conversations_setTopic(channel=cid, topic=topic)
        ):
            channel["topic"] = {"value": topic}
        if purpose and await step(
            "setting purpose", slack.conversations_setPurpose(channel=cid, purpose=purpose)
        ):
            channel["purpose"] = {"value": purpose}
        invite = [u for u in (args.get("invite_user_ids") or []) if isinstance(u, str) and u]
        if invite:
            await step("inviting users", slack.conversations_invite(channel=cid, users=invite))

        out: dict[str, Any] = {"channel": _channel_row(channel)}
        if invite:
            out["invited"] = invite
        if warnings:
            out["warnings"] = warnings
        return _ok(out)

    @tool(
        "archive_channel",
        "Archive a channel in this Slack workspace. Archived channels keep their "
        "history and a workspace member can unarchive them from Slack. Slack "
        "doesn't let bots delete channels outright. The bot must be a member of "
        "the channel. Only use this when the person asked for this channel to "
        "be archived.",
        {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel id (C… or G…) or name, with or without the leading #.",
                },
            },
            "required": ["channel"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    @_guard("conversations.archive")
    async def archive_channel(args: dict[str, Any]) -> dict[str, Any]:
        ref = (args.get("channel") or "").strip()
        if not ref:
            return _err("archive_channel needs a channel id or name")
        channel = await _resolve_channel(slack, ref)
        if channel is None:
            return _err(f"Can't archive {ref}: {_ARCHIVE_ERRORS['channel_not_found']}")
        row = _channel_row(channel)
        if row["is_archived"]:
            return _ok({"channel": row, "note": "already archived"})
        try:
            await slack.conversations_archive(channel=row["id"])
        except SlackApiError as e:
            data = e.response.data if isinstance(e.response.data, dict) else {}
            code = data.get("error", "unknown_error")
            if code == "already_archived":
                row["is_archived"] = True
                return _ok({"channel": row, "note": "already archived"})
            if code in _ARCHIVE_ERRORS:
                return _err(f"Can't archive #{row['name']}: {_ARCHIVE_ERRORS[code]}")
            raise
        log.info("archived channel %s (#%s)", row["id"], row["name"])
        row["is_archived"] = True
        return _ok({"channel": row})

    @tool(
        "post_message",
        "Post a message to a channel (optionally as a reply in a thread), with "
        "optional file attachments. Use it to write somewhere other than the "
        "conversation you're replying in: your reply there is posted for you. "
        "Mention people or agents with `<@USERID>` tokens in the text. The bot "
        "must be a member of the channel.",
        {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel id (C…/G…/D…) or name, with or without the leading #.",
                },
                "text": {"type": "string", "description": "Message text, Slack mrkdwn."},
                "thread_ts": {
                    "type": "string",
                    "description": "Reply in this thread (the parent message's ts). Omit for a top-level post.",
                },
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of local files to upload into the same place.",
                },
            },
            "required": ["channel", "text"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    @_guard("chat.postMessage")
    async def post_message(args: dict[str, Any]) -> dict[str, Any]:
        ref = (args.get("channel") or "").strip()
        text = (args.get("text") or "").strip()
        if not ref or not text:
            return _err("post_message needs a channel and some text")
        if re.fullmatch(r"D[A-Z0-9]{6,}", ref):
            channel_id = ref
        else:
            channel = await _resolve_channel(slack, ref)
            if channel is None:
                return _err(f"No channel {ref} that the bot can see")
            channel_id = channel["id"]
        thread_ts = (args.get("thread_ts") or "").strip() or None
        resp = await slack.chat_postMessage(channel=channel_id, text=text, thread_ts=thread_ts)
        out: dict[str, Any] = {"channel": channel_id, "ts": resp.get("ts")}
        paths = [p for p in (args.get("file_paths") or []) if isinstance(p, str) and p]
        if paths:
            await upload_files(slack, channel_id, thread_ts, paths)
            out["uploaded"] = paths
        log.info("posted to %s (ts=%s)", channel_id, out["ts"])
        return _ok(out)

    @tool(
        "read_messages",
        "Read recent messages from a channel, oldest first, or the replies in one "
        "thread (pass thread_ts). Use `oldest` to fetch only what's new since a ts "
        "you've already seen. The bot must be a member of the channel.",
        {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel id (C…/G…/D…) or name, with or without the leading #.",
                },
                "thread_ts": {"type": "string", "description": "Read this thread's replies instead of the channel."},
                "oldest": {"type": "string", "description": "Only messages after this ts."},
                "limit": {"type": "integer", "description": "How many messages (default 20, max 100)."},
            },
            "required": ["channel"],
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    @_guard("conversations.history")
    async def read_messages(args: dict[str, Any]) -> dict[str, Any]:
        ref = (args.get("channel") or "").strip()
        if not ref:
            return _err("read_messages needs a channel id or name")
        if re.fullmatch(r"D[A-Z0-9]{6,}", ref):
            channel_id = ref
        else:
            channel = await _resolve_channel(slack, ref)
            if channel is None:
                return _err(f"No channel {ref} that the bot can see")
            channel_id = channel["id"]
        limit = max(1, min(int(args.get("limit") or 20), 100))
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": limit}
        if args.get("oldest"):
            kwargs["oldest"] = str(args["oldest"])
        thread_ts = (args.get("thread_ts") or "").strip()
        if thread_ts:
            resp = await slack.conversations_replies(ts=thread_ts, **kwargs)
            msgs = resp.get("messages") or []  # already oldest first
        else:
            resp = await slack.conversations_history(**kwargs)
            msgs = list(reversed(resp.get("messages") or []))  # newest first from Slack
        return _ok({
            "channel": channel_id,
            "messages": [_message_row(m) for m in msgs],
            "has_more": bool(resp.get("has_more")),
        })

    @tool(
        "add_reaction",
        "Add an emoji reaction to a message. Needs the app's `reactions:write` scope.",
        {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel id (C…/G…/D…) or name, with or without the leading #.",
                },
                "timestamp": {"type": "string", "description": "The message's ts."},
                "name": {"type": "string", "description": "Emoji name without colons, e.g. white_check_mark."},
            },
            "required": ["channel", "timestamp", "name"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    @_guard("reactions.add")
    async def add_reaction(args: dict[str, Any]) -> dict[str, Any]:
        ref = (args.get("channel") or "").strip()
        ts = (args.get("timestamp") or "").strip()
        name = (args.get("name") or "").strip().strip(":")
        if not ref or not ts or not name:
            return _err("add_reaction needs a channel, timestamp and emoji name")
        if re.fullmatch(r"D[A-Z0-9]{6,}", ref):
            channel_id = ref
        else:
            channel = await _resolve_channel(slack, ref)
            if channel is None:
                return _err(f"No channel {ref} that the bot can see")
            channel_id = channel["id"]
        try:
            await slack.reactions_add(channel=channel_id, timestamp=ts, name=name)
        except SlackApiError as e:
            data = e.response.data if isinstance(e.response.data, dict) else {}
            if data.get("error") == "already_reacted":
                return _ok({"channel": channel_id, "ts": ts, "name": name, "note": "already reacted"})
            raise
        return _ok({"channel": channel_id, "ts": ts, "name": name})

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        tools=[
            list_channels,
            list_users,
            create_channel,
            archive_channel,
            post_message,
            read_messages,
            add_reaction,
        ],
    )
