"""Slack output helpers: streaming renderer, attachment up/download, tool labels.

Lifted from the original codespace agent-wrapper with no behavioural change — this is
the part that turns a Claude turn into nicely streamed Slack messages. Kept in
its own module so agent_wrapper.py stays focused on transport (register + WebSocket).
"""

from __future__ import annotations

import logging
import os
import random
import re
import tempfile
import time
from typing import Any, Optional

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger("agent-wrapper.slack")

MAX_MSG_CHARS = 2800
MIN_UPDATE_INTERVAL_S = 1.0

# A line like `ATTACH: /abs/path/to/file` flags a file to upload to the thread.
ATTACH_RE = re.compile(r"^[ \t]*ATTACH:[ \t]*(.+?)[ \t]*$", re.MULTILINE)

THINKING_PHRASES = (
    "cogitatating...",
    "ruminapping...",
    "ponderizing...",
    "syllogisifying...",
    "deliberatening...",
    "hypothesnoozing...",
    "percologitating...",
    "conflabulating...",
    "noodlebrating...",
    "mullingate...",
    "brainstorbling...",
    "contemplifying...",
    "inferenciating...",
    "metacognizzling...",
    "epiphanizing...",
    "deducticating...",
    "cerebrolating...",
    "reckonifying...",
    "surmisening...",
)


class SlackRenderer:
    """Compact streaming renderer: post a placeholder, then edit it in place as
    the agent's text accumulates, rolling over to a new message before Slack's
    length limit."""

    def __init__(
        self,
        client: AsyncWebClient,
        channel: str,
        thread_ts: Optional[str],
        queued: bool = False,
    ):
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._ts: Optional[str] = None
        self._body = ""
        self._last_flushed_len = 0
        self._last_flush_at = 0.0
        # If another turn on this thread is still running, our turn waits behind
        # it — show that instead of a frozen "thinking…" line. Swapped for a
        # normal phrase by mark_active() once our turn actually starts.
        self._queued = queued
        self._placeholder = (
            "_⏳ queued — finishing an earlier request in this thread first…_"
            if queued
            else f"_{random.choice(THINKING_PHRASES)}_"
        )
        # Latest tool activity ("📄 Read foo.py"), shown while the agent works so
        # a long tool run isn't dead air. Cleared when real text resumes.
        self._status = ""

    async def mark_active(self) -> None:
        """The turn just started (we hold the session lock now). If we're still
        showing the queued placeholder and nothing real has streamed yet, swap
        it for a normal thinking phrase so the message doesn't read as queued."""
        if not self._queued:
            return
        self._queued = False
        if self._body or self._status:
            return
        self._placeholder = f"_{random.choice(THINKING_PHRASES)}_"
        await self._push(force=True)

    async def open(self) -> None:
        # A Slack failure must never escape the renderer: callers run this from
        # inside the loop consuming a turn's message stream, and an exception
        # there abandons the stream mid-turn, which permanently desyncs every
        # later turn's output (see Session.send). Failing to post just means we
        # render nothing — `_ts is None` makes every later write a no-op.
        try:
            resp = await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text=self._placeholder
            )
        except SlackApiError as e:
            log.warning("chat_postMessage (open) failed; rendering nothing: %s", e)
            return
        self._ts = resp["ts"]

    def _render(self) -> str:
        body = self._body.strip()
        if self._status:
            return f"{body}\n\n_{self._status}_" if body else f"_{self._status}_"
        return body or self._placeholder

    async def append(self, text: str) -> None:
        if not text:
            return
        self._status = ""
        # Spread `text` across as many messages as needed so no single Slack
        # message exceeds MAX_MSG_CHARS (a whole reply can arrive in one chunk).
        while text:
            capacity = MAX_MSG_CHARS - len(self._body)
            if capacity <= 0:
                await self.flush(force=True)
                await self._roll_over()
                continue
            if len(text) <= capacity:
                self._body += text
                break
            cut = self._split_point(text, capacity)
            self._body += text[:cut]
            text = text[cut:]
            await self.flush(force=True)
            await self._roll_over()
        await self.flush()

    @staticmethod
    def _split_point(text: str, capacity: int) -> int:
        window = text[:capacity]
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > 0:
                return idx + len(sep)
        return capacity

    async def status(self, label: str) -> None:
        if not label or label == self._status:
            return
        self._status = label
        await self._push(force_status=True)

    async def flush(self, force: bool = False) -> None:
        await self._push(force=force)

    async def _push(self, force: bool = False, force_status: bool = False) -> None:
        if self._ts is None:
            return
        now = time.monotonic()
        if not force:
            if not force_status and len(self._body) - self._last_flushed_len < 120:
                return
            if now - self._last_flush_at < MIN_UPDATE_INTERVAL_S:
                return
        try:
            await self._client.chat_update(
                channel=self._channel, ts=self._ts, text=self._render()
            )
            self._last_flushed_len = len(self._body)
            self._last_flush_at = now
        except SlackApiError as e:
            log.warning("chat_update failed: %s", e)

    async def _roll_over(self) -> None:
        # Same rule as open(): never raise into the turn's stream consumer. If the
        # continuation message can't be posted, stop rendering (`_ts = None`)
        # rather than overwriting the message we just filled with the next slice.
        try:
            resp = await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text="…"
            )
        except SlackApiError as e:
            log.warning("chat_postMessage (roll-over) failed; dropping the rest: %s", e)
            self._ts = None
        else:
            self._ts = resp["ts"]
        self._body = ""
        self._last_flushed_len = 0
        self._last_flush_at = 0.0

    async def replace_with(self, text: str) -> None:
        if self._ts is None:
            return
        try:
            await self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
        except SlackApiError:
            log.exception("chat_update (error message) failed")


async def download_slack_files(files: list[dict[str, Any]], token: str) -> list[str]:
    """Download Slack file attachments (url_private needs the bot token) into a
    temp dir; return the local paths so the agent can read them."""
    out: list[str] = []
    dirp = os.path.join(tempfile.gettempdir(), "slack-attachments")
    os.makedirs(dirp, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as http:
        for f in files:
            url = f.get("url_private")
            if not url:
                continue
            name = f.get("name") or f.get("id") or "file"
            dest = os.path.join(dirp, f"{f.get('id', 'f')}-{name}")
            try:
                async with http.get(url, headers=headers) as r:
                    r.raise_for_status()
                    data = await r.read()
                with open(dest, "wb") as fh:
                    fh.write(data)
                out.append(dest)
                log.info("downloaded attachment %s -> %s", name, dest)
            except Exception as e:  # noqa: BLE001 — one bad file shouldn't fail the turn
                log.warning("attachment download failed for %s: %s", url, e)
    return out


async def upload_files(
    slack: AsyncWebClient, channel: str, thread_ts: Optional[str], paths: list[str]
) -> None:
    for path in paths:
        p = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
        if not os.path.isfile(p):
            log.warning("ATTACH: file not found: %s", p)
            continue
        try:
            await slack.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=p,
                filename=os.path.basename(p),
            )
            log.info("uploaded %s to channel=%s", p, channel)
        except Exception:  # noqa: BLE001
            log.exception("files_upload_v2 failed for %s", p)


def _clip(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_label(name: Optional[str], args: Optional[dict]) -> str:
    """A short, human-friendly status line for a tool call — shown in the Slack
    message while the tool runs."""
    name = name or "tool"
    a = args or {}
    desc = a.get("description")
    if isinstance(desc, str) and desc.strip():
        return f"🔧 {_clip(desc)}"
    if name == "Bash":
        return f"💻 {_clip(str(a.get('command', '')))}"
    if name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = a.get("file_path") or a.get("notebook_path") or ""
        return f"📄 {name} {_clip(os.path.basename(str(path)) or str(path))}"
    if name == "WebFetch":
        return f"🌐 fetching {_clip(str(a.get('url', '')))}"
    if name in ("Grep", "Glob"):
        return f"🔎 {name} {_clip(str(a.get('pattern') or a.get('query') or ''))}"
    if name == "Task":
        return f"🤖 {_clip(str(a.get('description') or 'subagent'))}"
    if name == "WebSearch":
        return f"🔎 searching {_clip(str(a.get('query', '')))}"
    return f"🔧 {name}"
