"""Outgoing-text mention fix-ups, applied to everything the agent posts.

Slack only notifies someone when the message text carries a real mention
token, `<@U0123>`. Models are unreliable at producing that: they write the
natural `@builder`, or they HTML-escape the token (`&lt;@U0123&gt;`), and
either way the message renders as plain text and wakes nobody — which, for a
pipeline of agents that only act when mentioned, means the hand-off is lost.

So the bridge repairs both on the way out:

- `&lt;@U0123&gt;` (and `&lt;!here&gt;`-style specials) become `<@U0123>`.
- `@handle`, where `handle` is a Slack username or display name in this
  workspace, becomes `<@U0123>`. Unknown handles, e-mail addresses and
  anything inside code spans or blocks are left alone.

`chat.postMessage`'s `link_names` used to do the second part but no longer
links individual users, hence our own directory, filled from `users.list`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

log = logging.getLogger("agent-wrapper.mentions")

# A mention token the model HTML-escaped. Slack would show it literally.
_ESCAPED_TOKEN_RE = re.compile(
    r"&lt;(@[UW][A-Z0-9]+|!(?:here|channel|everyone|subteam\^[A-Z0-9]+))(\|[^&<>\n]*)?&gt;"
)

# Fenced blocks and inline code: never rewrite inside these.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)

# `@handle` — Slack usernames are letters, digits, `.`, `-`, `_`. Not preceded
# by a word character (e-mail addresses), `<` or `@` (an existing token),
# `|` (a link label) or `/` (a URL path). The match may swallow trailing
# sentence punctuation like `@builder.` — the resolver retries without it.
_HANDLE_RE = re.compile(r"(?<![\w<@|/])@([A-Za-z0-9][A-Za-z0-9._-]{0,79})")

# Full re-fetch of users.list this often; sooner on an unknown handle, but at
# most once a minute so a stream of unknown `@foo`s can't hammer the API.
_USERS_TTL_S = 3600.0
_MISS_REFRESH_MIN_S = 60.0


def unescape_mention_tokens(text: str) -> str:
    """`&lt;@U0123&gt;` -> `<@U0123>`. Only mention/special tokens; other
    escaped angle brackets are left as the model wrote them."""
    if "&lt;" not in text:
        return text
    return _ESCAPED_TOKEN_RE.sub(lambda m: f"<{m.group(1)}{m.group(2) or ''}>", text)


def _handle_map(members: list[dict]) -> dict[str, str]:
    """handle (lower-cased) -> user id. Usernames take precedence over display
    names, so a display name can't hijack someone else's handle. Display names
    with whitespace can't be typed as `@x` and are skipped."""
    by_handle: dict[str, str] = {}
    for u in members:
        if u.get("deleted") or not u.get("id"):
            continue
        name = (u.get("name") or "").strip().lower()
        if name:
            by_handle.setdefault(name, u["id"])
    for u in members:
        if u.get("deleted") or not u.get("id"):
            continue
        disp = ((u.get("profile") or {}).get("display_name") or "").strip().lower()
        if disp and not any(ch.isspace() for ch in disp):
            by_handle.setdefault(disp, u["id"])
    return by_handle


class MentionDirectory:
    """Cached `users.list` for resolving `@handle` to a user id."""

    def __init__(self, slack: AsyncWebClient):
        self._slack = slack
        self._by_handle: dict[str, str] = {}
        self._fetched_at = 0.0
        self._last_miss_refresh = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        async with self._lock:
            members: list[dict] = []
            cursor = None
            try:
                while True:
                    resp = await self._slack.users_list(limit=200, cursor=cursor)
                    members.extend(resp.get("members") or [])
                    cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "")
                    if not cursor:
                        break
            except Exception:  # noqa: BLE001 — a failed refresh mustn't block a post
                log.warning("users.list failed; keeping the cached handle map", exc_info=True)
                self._fetched_at = time.monotonic()  # back off, don't retry every call
                return
            self._by_handle = _handle_map(members)
            self._fetched_at = time.monotonic()
            log.info("mention directory: %d handles", len(self._by_handle))

    async def lookup(self, handle: str) -> Optional[str]:
        """User id for a handle (case-insensitive), or None."""
        h = handle.lower()
        now = time.monotonic()
        if now - self._fetched_at > _USERS_TTL_S:
            await self.refresh()
        uid = self._by_handle.get(h)
        if uid is None and now - self._last_miss_refresh > _MISS_REFRESH_MIN_S:
            # Maybe a user who joined since the last fetch; look once.
            self._last_miss_refresh = now
            await self.refresh()
            uid = self._by_handle.get(h)
        return uid

    async def resolve(self, text: str) -> str:
        """Repair escaped tokens and turn known `@handle`s into `<@U…>`."""
        if not text:
            return text
        text = unescape_mention_tokens(text)
        if "@" not in text:
            return text

        # Split into (segment, is_code) so code stays untouched.
        parts: list[tuple[str, bool]] = []
        pos = 0
        for m in _CODE_RE.finditer(text):
            parts.append((text[pos : m.start()], False))
            parts.append((m.group(0), True))
            pos = m.end()
        parts.append((text[pos:], False))

        candidates: set[str] = set()
        for seg, is_code in parts:
            if is_code:
                continue
            for m in _HANDLE_RE.finditer(seg):
                h = m.group(1)
                candidates.add(h)
                candidates.add(h.rstrip("._-"))
        resolved: dict[str, str] = {}
        for c in sorted(candidates):
            if not c:
                continue
            uid = await self.lookup(c)
            if uid:
                resolved[c.lower()] = uid

        if not resolved:
            return text

        def repl(m: re.Match) -> str:
            h = m.group(1)
            uid = resolved.get(h.lower())
            if uid:
                return f"<@{uid}>"
            core = h.rstrip("._-")
            uid = resolved.get(core.lower())
            if uid:
                return f"<@{uid}>{h[len(core):]}"
            return m.group(0)

        return "".join(seg if is_code else _HANDLE_RE.sub(repl, seg) for seg, is_code in parts)


_directories: dict[int, MentionDirectory] = {}


def directory_for(slack: AsyncWebClient) -> MentionDirectory:
    """One directory per Slack client (one per workspace in practice)."""
    d = _directories.get(id(slack))
    if d is None:
        d = _directories[id(slack)] = MentionDirectory(slack)
    return d
