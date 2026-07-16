"""Per-Slack-thread persistent Claude sessions.

Each session wraps a long-lived `ClaudeSDKClient`, which itself wraps a
persistent `claude` CLI subprocess speaking stream-json over stdin/stdout.
Turns are serialized per session via an asyncio.Lock so concurrent Slack
events on the same thread don't interleave on one stdin pipe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, replace
from typing import AsyncIterator, Callable, Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

log = logging.getLogger("claude-slack-bot.session")


def load_cli_mcp_servers(cwd: Optional[str] = None) -> dict[str, dict]:
    """Return the MCP servers configured for the `claude` CLI so the Agent SDK
    can use the same ones.

    The SDK starts its own `claude` subprocess and does NOT inherit the MCP
    servers you've added with `claude mcp add` — those live in `~/.claude.json`
    (and project `.mcp.json` files), which the SDK never reads on its own. We
    load them here and pass them to ClaudeAgentOptions(mcp_servers=...).

    Sources, merged in increasing precedence:
      1. user scope     — top-level `mcpServers` in ~/.claude.json
      2. project scope  — `mcpServers` under projects[cwd] in ~/.claude.json
      3. project file   — a `.mcp.json` ({"mcpServers": {...}}) in cwd

    The entry shapes in these files ({"type": "stdio"|"http"|"sse", "command",
    "args", "env", "url", "headers"}) already match what the SDK expects, so we
    pass them straight through.
    """
    servers: dict[str, dict] = {}
    home_config = os.path.expanduser("~/.claude.json")
    try:
        with open(home_config) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError) as e:
        log.info("no claude CLI config at %s (%s); no MCP servers loaded", home_config, e)
        data = {}

    servers.update(data.get("mcpServers") or {})

    if cwd:
        project = (data.get("projects") or {}).get(os.path.abspath(cwd)) or {}
        servers.update(project.get("mcpServers") or {})

        dot_mcp = os.path.join(cwd, ".mcp.json")
        try:
            with open(dot_mcp) as f:
                servers.update(json.load(f).get("mcpServers") or {})
        except (FileNotFoundError, ValueError, OSError):
            pass

    if servers:
        log.info("loaded %d MCP server(s) from claude CLI config: %s",
                 len(servers), ", ".join(sorted(servers)))
    return servers

# Thread -> claude session_id map, persisted on the /workspaces volume (next to
# this file) so it survives a codespace stop/start. Lets a restarted bridge
# RESUME each thread's prior conversation (full transcript) instead of starting
# a blank one. (A rebuild wipes ~/.claude transcripts, so resume falls back to a
# fresh session there — handled in Session._ensure_connected.)
_SESSION_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sessions.json")

# Prepended to the first prompt after a resume so the agent re-orients instead of
# acting like a brand-new conversation (the prior transcript is already loaded).
_RESUME_PRIMER = (
    "[Resuming this thread after the workspace was paused and restarted. The full "
    "conversation above is yours — you have all prior context, including anything "
    "you set up or left running. Continue from where you left off; if you were "
    "mid-task, pick it back up. Don't re-introduce yourself or restate everything "
    "— just carry on.]\n\n"
)


@dataclass
class Chunk:
    """One piece of output produced during a turn.

    `kind` is one of:
      - "text"        — assistant text block, suitable for streaming to Slack.
      - "tool_use"    — formatted tool invocation (name + full args) for the transcript.
      - "tool_result" — formatted tool result (id, is_error, full content) for the transcript.
      - "result"      — terminal turn summary (duration / cost / usage / error).

    The optional `name` / `args` / `is_error` fields are populated for
    `tool_use` and `tool_result` chunks so consumers can format short-form
    summaries without re-parsing `text`.
    """

    kind: str
    text: str
    name: Optional[str] = None
    args: Optional[dict] = None
    is_error: bool = False


class Session:
    def __init__(
        self,
        key: str,
        options: ClaudeAgentOptions,
        resumed: bool = False,
        on_session_id: Optional[Callable[[str, str], None]] = None,
    ):
        self.key = key
        self.log = logging.getLogger(f"claude-slack-bot.session[{key}]")
        self._options = options
        self._client = ClaudeSDKClient(options=options)
        self._connected = False
        self._lock = asyncio.Lock()
        self._turn = 0
        # `resumed` => the prior transcript is being reloaded; prime the agent on
        # the first turn. `on_session_id` persists the (possibly new) session id.
        self._resumed = resumed
        self._primer_pending = resumed
        self._on_session_id = on_session_id
        self.session_id: Optional[str] = getattr(options, "resume", None)

    @property
    def next_turn_number(self) -> int:
        """The 1-based turn number that the next call to `send()` will use."""
        return self._turn + 1

    def _capture_session_id(self, sid: Optional[str]) -> None:
        if sid and sid != self.session_id:
            self.session_id = sid
            if self._on_session_id:
                try:
                    self._on_session_id(self.key, sid)
                except Exception:  # noqa: BLE001 — persistence must not break a turn
                    self.log.warning("could not persist session id", exc_info=True)

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        self.log.info("connecting claude subprocess%s", " (resume)" if self._resumed else "")
        try:
            await self._client.connect()
        except Exception as e:  # noqa: BLE001
            if not self._resumed:
                raise
            # The transcript is gone (e.g. after a rebuild) — drop resume and
            # start fresh rather than failing the turn.
            self.log.warning("resume failed (%s); starting a fresh session", e)
            self._resumed = False
            self._primer_pending = False
            self._client = ClaudeSDKClient(options=replace(self._options, resume=None))
            await self._client.connect()
        self._connected = True
        self.log.info("connected")

    async def send(self, prompt: str) -> AsyncIterator[Chunk]:
        async with self._lock:
            await self._ensure_connected()
            self._turn += 1
            turn = self._turn
            if self._primer_pending:
                # Only on the first turn after a resume (and only if the resume
                # actually took — _ensure_connected clears it on fallback).
                if self._resumed:
                    prompt = _RESUME_PRIMER + prompt
                    self.log.info("turn %d: prepended resume primer", turn)
                self._primer_pending = False
            self.log.info(
                "turn %d: user prompt (%d chars): %r",
                turn, len(prompt), _truncate(prompt, 200),
            )
            await self._client.query(prompt)
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            self.log.info(
                                "turn %d: assistant text (%d chars): %r",
                                turn, len(block.text), _truncate(block.text, 200),
                            )
                            self.log.debug("turn %d: full text: %s", turn, block.text)
                            yield Chunk("text", block.text)
                        elif isinstance(block, ToolUseBlock):
                            self.log.info(
                                "turn %d: tool_use %s id=%s args=%s",
                                turn, block.name, block.id,
                                _truncate(repr(block.input or {}), 400),
                            )
                            yield Chunk(
                                "tool_use",
                                _format_tool_use_full(block),
                                name=block.name,
                                args=dict(block.input or {}),
                            )
                        elif isinstance(block, ThinkingBlock):
                            self.log.debug(
                                "turn %d: thinking (%d chars): %s",
                                turn, len(block.thinking),
                                _truncate(block.thinking, 500),
                            )
                elif isinstance(msg, UserMessage):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            content = block.content
                            if isinstance(content, list):
                                content_str = " ".join(
                                    getattr(c, "text", repr(c)) for c in content
                                )
                            else:
                                content_str = str(content)
                            level = (
                                logging.WARNING if block.is_error else logging.INFO
                            )
                            self.log.log(
                                level,
                                "turn %d: tool_result id=%s is_error=%s "
                                "(%d chars): %r",
                                turn, block.tool_use_id, block.is_error,
                                len(content_str), _truncate(content_str, 200),
                            )
                            self.log.debug(
                                "turn %d: tool_result full: %s", turn, content_str,
                            )
                            yield Chunk(
                                "tool_result",
                                _format_tool_result(
                                    block.tool_use_id, block.is_error, content_str
                                ),
                                is_error=bool(block.is_error),
                            )
                elif isinstance(msg, SystemMessage):
                    data = getattr(msg, "data", {}) or {}
                    # The init message carries the session id — capture it early
                    # so even a mid-turn shutdown leaves a resumable id on disk.
                    self._capture_session_id(data.get("session_id"))
                    self.log.debug(
                        "turn %d: system subtype=%s data=%s",
                        turn, getattr(msg, "subtype", "?"),
                        _truncate(repr(data), 300),
                    )
                elif isinstance(msg, ResultMessage):
                    self._capture_session_id(getattr(msg, "session_id", None))
                    is_err = getattr(msg, "is_error", False)
                    duration = getattr(msg, "duration_ms", None)
                    cost = getattr(msg, "total_cost_usd", None)
                    usage = getattr(msg, "usage", None)
                    self.log.info(
                        "turn %d: result is_error=%s duration_ms=%s cost_usd=%s "
                        "usage=%s",
                        turn, is_err, duration, cost,
                        _truncate(repr(usage), 200) if usage else None,
                    )
                    parts: list[str] = []
                    if is_err:
                        parts.append(f"ERROR: {getattr(msg, 'result', '')}")
                    if duration is not None:
                        parts.append(f"duration={duration}ms")
                    if cost is not None:
                        parts.append(f"cost=${cost}")
                    if usage is not None:
                        parts.append(f"usage={usage}")
                    yield Chunk("result", " ".join(parts))
                    return

    async def close(self) -> None:
        if self._connected:
            self.log.info("disconnecting")
            try:
                await self._client.disconnect()
            finally:
                self._connected = False
                self.log.info("closed")

    async def interrupt(self) -> bool:
        """Cancel the turn currently in flight, if any. Returns whether there
        was one to cancel.

        Deliberately does NOT take `_lock` — that's held by `send()` for the
        whole turn, so waiting on it here would block until the turn finishes
        on its own, defeating the point. `_client.interrupt()` is a control-
        channel request independent of the stdout stream `send()` is reading,
        so it's safe to fire from a concurrent task."""
        if not self._connected or not self._lock.locked():
            return False
        await self._client.interrupt()
        return True


_MAX_TOOL_RESULT_CHARS = 32_000


def _format_tool_use_full(block: ToolUseBlock) -> str:
    """Verbose tool-call entry for the per-turn transcript file."""
    import json
    args = block.input or {}
    try:
        rendered = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except Exception:
        rendered = repr(args)
    return f"[tool_use] {block.name} id={block.id}\n{rendered}"


def _format_tool_result(tool_use_id: str, is_error: bool, content: str) -> str:
    head = f"[tool_result] id={tool_use_id} is_error={is_error} chars={len(content)}"
    body = content
    if len(body) > _MAX_TOOL_RESULT_CHARS:
        body = body[:_MAX_TOOL_RESULT_CHARS] + f"\n…[truncated, +{len(content) - _MAX_TOOL_RESULT_CHARS} chars]"
    return f"{head}\n{body}"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class SessionManager:
    def __init__(
        self,
        cwd: str,
        permission_mode: str,
        model: Optional[str] = None,
        setting_sources: Optional[list[str]] = None,
        extra_args: Optional[dict[str, Optional[str]]] = None,
        system_prompt_append: Optional[str] = None,
        mcp_servers: Optional[dict[str, dict]] = None,
    ):
        self._cwd = cwd
        self._permission_mode = permission_mode
        self._model = model
        self._setting_sources = setting_sources
        self._extra_args = extra_args or {}
        self._mcp_servers = mcp_servers or {}
        self._system_prompt_append = system_prompt_append
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        # thread_key -> claude session_id, loaded from the /workspaces volume so a
        # restarted bridge can resume each thread's conversation.
        self._resume_ids: dict[str, str] = self._load_store()
        if self._resume_ids:
            log.info("loaded %d resumable session(s) from %s", len(self._resume_ids), _SESSION_STORE)

    def _load_store(self) -> dict[str, str]:
        try:
            with open(_SESSION_STORE) as f:
                data = json.load(f)
            return {str(k): str(v) for k, v in data.items() if v}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_store(self) -> None:
        try:
            tmp = _SESSION_STORE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._resume_ids, f)
            os.replace(tmp, _SESSION_STORE)
        except OSError as e:
            log.warning("could not persist session map: %s", e)

    def _remember_session_id(self, key: str, sid: str) -> None:
        if self._resume_ids.get(key) == sid:
            return
        self._resume_ids[key] = sid
        self._save_store()
        log.info("session map: %s -> %s", key, sid)

    def _build_options(self, resume: Optional[str] = None) -> ClaudeAgentOptions:
        kwargs: dict = {
            "cwd": self._cwd,
            "permission_mode": self._permission_mode,
        }
        if resume:
            kwargs["resume"] = resume
        if self._model:
            kwargs["model"] = self._model
        if self._setting_sources:
            kwargs["setting_sources"] = self._setting_sources
        if self._mcp_servers:
            kwargs["mcp_servers"] = self._mcp_servers
        if self._extra_args:
            kwargs["extra_args"] = dict(self._extra_args)
        if self._system_prompt_append:
            kwargs["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": self._system_prompt_append,
            }
        return ClaudeAgentOptions(**kwargs)

    async def get_or_create(self, key: str) -> Session:
        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                resume_id = self._resume_ids.get(key)
                if resume_id:
                    log.info("resuming thread %s from session %s", key, resume_id)
                session = Session(
                    key,
                    self._build_options(resume=resume_id),
                    resumed=bool(resume_id),
                    on_session_id=self._remember_session_id,
                )
                self._sessions[key] = session
            return session

    def exists(self, key: str) -> bool:
        return key in self._sessions

    def get(self, key: str) -> Optional[Session]:
        """Look up a thread's session without creating one, so callers that
        only want to act on an existing turn (e.g. !stop) don't spin up a
        fresh session just to find nothing running in it."""
        return self._sessions.get(key)

    async def drop(self, key: str) -> bool:
        """Close and forget the session for `key`. Returns True if one existed.

        Also forgets the persisted resume id so a later message starts a fresh
        conversation rather than resuming the cleared one."""
        async with self._lock:
            session = self._sessions.pop(key, None)
            had_resume = self._resume_ids.pop(key, None) is not None
            if had_resume:
                self._save_store()
        if session is None:
            return had_resume
        await session.close()
        return True

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
