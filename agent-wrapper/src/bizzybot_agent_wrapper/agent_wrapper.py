"""Bizzybot OSS agent-wrapper.

Runs in the agent workspace (a laptop, VM, or container — set up by hand). It:
  1. dials home to Central-Dispatch with a registration token and pulls the Slack bot
     token + WebSocket details (see central-dispatch/docs, "Registration (dial-home)");
  2. runs a local preflight (Claude Code, gh, git);
  3. opens a WebSocket to Central-Dispatch, replays anything it missed while offline
     (via lastSeq), then processes live Slack events;
  4. drives one persistent Claude Code session per Slack thread and posts the
     replies straight back to Slack.

No Ably, no cloud provisioning. On macOS it runs `caffeinate` so an idle laptop
doesn't sleep and drop the connection (see _prevent_sleep_on_macos). Uses your
own local git/gh auth. Installed as the `bizzybot` command (see pyproject
[project.scripts]); run from source with `uv run bizzybot`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable, Optional

import ssl

import aiohttp
from dotenv import load_dotenv
from slack_sdk.web.async_client import AsyncWebClient

from . import email_reply, pr_poller, sentry_poller
from .paths import log_path, state_path
from .session_manager import SessionManager, load_cli_mcp_servers
from .settings import claude_env, load_settings, resolve
from .slack_io import SlackRenderer, download_slack_files, upload_files, tool_label, ATTACH_RE

load_dotenv()

log = logging.getLogger("agent-wrapper")

# Per-user state dir (~/.bizzybot by default) — see paths.state_path.
STATE_PATH = state_path("agent-wrapper-state.json")
CONFIG_PATH = state_path("agent-wrapper-config.json")

# Default hosted Central-Dispatch. Override with CENTRAL_URL (env/.env) or the saved config.
DEFAULT_CENTRAL_URL = "https://claudebot-production-34ba.up.railway.app"

SLACK_FORMATTING_PROMPT = """\
Your replies are posted directly to Slack. Format every response in Slack's
"mrkdwn" dialect, not standard or GitHub-flavored Markdown:
- Bold: *single asterisks* (NOT **double**). Italic: _underscores_.
- Inline code: `backticks`. Code blocks: triple backticks (no language tag).
- Links: <https://example.com|label> — NEVER [label](url).
- Headings (#, ##) do not render — use a *bold* line instead.
Keep output tight: Slack threads are narrow.

To attach a file (screenshot, image, document) to your reply, save it to disk
and put its absolute path on its own line prefixed with `ATTACH:`, e.g.:
  ATTACH: /path/to/shot.png
Emit one ATTACH line per file. The file is uploaded to this thread; the ATTACH
line itself is removed from your message, so write a normal sentence too."""

# Every thread runs in the same cwd (see build_session_manager), concurrently, so
# the agent has to isolate its own edits and clean up what it starts. Advisory —
# nothing enforces it.
WORKSPACE_PROMPT = """\
Every Slack thread shares ONE working directory, and other threads may be active in
it at the same time — you don't have exclusive control of it. In a git repo, never
switch branches in the shared tree; another thread's edits would land on your branch.

So before you edit code, give yourself an isolated tree:
  git worktree add ../<short-name> -b <branch>
cd into it and do all of this thread's work there, reusing that one worktree on
later turns instead of creating another. Reading and inspecting the shared tree is
fine — only edits need a worktree.

Tear both down when you're finished:
- Once your changes are committed (and pushed, if you're pushing), `cd` out and
  `git worktree remove <path>` — don't leave the worktree lying around. If you
  abandon the work instead, remove it too.
- Kill every dev server, file watcher, or other background process you started to
  test something. They outlive your turn and hold their ports otherwise.

Do this before you report back, and say what you cleaned up."""


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _slack_escape(s: str) -> str:
    """Escape the three characters Slack treats as markup. `&` must go first or
    it would double-escape the entities added after it. PR titles and email
    subjects routinely contain `<T>`, `a -> b`, and `&`."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _insecure_tls_ctx(url: str):
    """For a local Central-Dispatch over TLS (https/wss to localhost with a self-signed
    cert), return an SSL context that skips verification. Returns None for plain
    http/ws or real remote hosts, which keep normal verification.

    Enable for non-localhost hosts with BIZZYBOT_INSECURE_TLS=1 if needed.
    """
    if not (url.startswith("https") or url.startswith("wss")):
        return None
    is_local = "localhost" in url or "127.0.0.1" in url
    if not (is_local or _truthy(os.getenv("BIZZYBOT_INSECURE_TLS"))):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_sources(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


# --- Config / registration --------------------------------------------------


class AuthError(Exception):
    """Central-Dispatch rejected the registration token."""


def _load_local_config() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_local_config(cfg: dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f)
        os.chmod(tmp, 0o600)  # holds the registration token (a secret)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log.warning("could not save agent-wrapper config: %s", e)


def _forget_token() -> None:
    cfg = _load_local_config()
    cfg.pop("registration_token", None)
    _save_local_config(cfg)


def resolve_central_dispatch() -> str:
    """CENTRAL_URL env > saved config > the default hosted Central-Dispatch."""
    saved = _load_local_config().get("central_url")
    return (os.getenv("CENTRAL_URL") or saved or DEFAULT_CENTRAL_URL).rstrip("/")


def resolve_token(central_dispatch: str) -> tuple[str, bool]:
    """Return (token, from_env). Reads REGISTRATION_TOKEN, then the saved config,
    then prompts interactively (caching the entered token for next time)."""
    env = os.getenv("REGISTRATION_TOKEN")
    if env:
        return env.strip(), True
    saved = _load_local_config().get("registration_token")
    if saved:
        return saved, False
    if not sys.stdin.isatty():
        sys.exit(
            "No registration token found. Set REGISTRATION_TOKEN (env/.env), or run "
            "the agent-wrapper interactively so it can prompt for one.\n"
            f"Get a token: sign in at {central_dispatch} and open your workspace dashboard."
        )
    token = _prompt_for_token(central_dispatch)
    _save_local_config({"registration_token": token, "central_url": central_dispatch})
    return token, False


def _prompt_for_token(central_dispatch: str) -> str:
    print("\n  Bizzybot agent-wrapper — first-time setup")
    print(f"  Central-Dispatch: {central_dispatch}")
    print(f"  Sign in at {central_dispatch} and open your dashboard to copy your registration token.\n")
    try:
        token = input("  Paste your registration token: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted")
    if not token:
        sys.exit("no token entered")
    return token


async def register(http: aiohttp.ClientSession, central_dispatch: str, token: str) -> dict[str, Any]:
    """Dial home: exchange the registration token for the Slack bot token and
    WebSocket connection details. Raises AuthError if the token is rejected."""
    async with http.post(
        f"{central_dispatch}/api/register", json={"token": token}, ssl=_insecure_tls_ctx(central_dispatch)
    ) as r:
        if r.status == 401:
            raise AuthError(await r.text())
        if r.status != 200:
            body = await r.text()
            sys.exit(f"Registration failed (HTTP {r.status}): {body}")
        data = await r.json()
    log.info("registered as agent %s", data.get("agentId"))
    return data


# --- Preflight --------------------------------------------------------------


def preflight() -> None:
    """Verify the local tools the agent needs. Fatal if Claude Code is missing;
    warnings otherwise (git ops may still work with your existing setup)."""
    problems: list[str] = []
    warnings: list[str] = []

    if shutil.which("claude"):
        log.info("preflight: claude ✓")
    else:
        problems.append(
            "Claude Code CLI not found on PATH. Install it: https://claude.com/product/claude-code"
        )

    gh = shutil.which("gh")
    if not gh:
        warnings.append("`gh` not found on PATH — GitHub operations may not work.")
    else:
        r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
        if r.returncode == 0:
            log.info("preflight: gh authenticated ✓")
        else:
            warnings.append("`gh` is installed but not authenticated. Run: gh auth login")

    if not shutil.which("git"):
        warnings.append("git not found on PATH.")
    else:
        name = subprocess.run(
            ["git", "config", "--global", "user.name"], capture_output=True, text=True
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "--global", "user.email"], capture_output=True, text=True
        ).stdout.strip()
        if name and email:
            log.info("preflight: git identity ✓ (%s <%s>)", name, email)
        else:
            warnings.append(
                "git identity not set. Run: "
                'git config --global user.name "You" && git config --global user.email you@example.com'
            )

    for w in warnings:
        log.warning("preflight: %s", w)
    if problems:
        for p in problems:
            log.error("preflight: %s", p)
        sys.exit("Preflight failed — fix the above and restart the agent-wrapper.")


# --- Session manager --------------------------------------------------------


def build_session_manager(settings: Optional[dict[str, str]] = None) -> SessionManager:
    extra_args: dict[str, str | None] = {}
    # Chrome (Claude-in-Chrome browser MCP) is on by default so target envs don't
    # each have to set it; disable explicitly with CLAUDE_CHROME=0.
    if _truthy(os.getenv("CLAUDE_CHROME", "1")):
        extra_args["chrome"] = None
    # Default to the directory bizzybot was launched from; CLAUDE_CWD overrides.
    cwd = os.getenv("CLAUDE_CWD") or os.getcwd()
    mcp_servers = (
        load_cli_mcp_servers(cwd) if _truthy(os.getenv("CLAUDE_LOAD_CLI_MCP", "1")) else {}
    )
    # The user's settings file (~/.bizzybot/settings.env) — passed through to
    # each claude subprocess, and the source of an alternate model provider.
    # main() loads it once and passes it in, so it isn't parsed (and logged) twice.
    env, provider_model = claude_env(load_settings() if settings is None else settings)
    model = os.getenv("CLAUDE_MODEL") or None
    if provider_model:
        # An Anthropic model id would 404 at OpenRouter, so the provider's model
        # wins over a CLAUDE_MODEL left over from a previous setup.
        if model and model != provider_model:
            log.warning(
                "CLAUDE_MODEL=%s ignored — OPENROUTER_MODEL=%s takes precedence",
                model, provider_model,
            )
        model = provider_model
    return SessionManager(
        cwd=cwd,
        permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
        model=model,
        setting_sources=_parse_sources(
            os.getenv("CLAUDE_SETTING_SOURCES", "user,project,local")
        ),
        extra_args=extra_args,
        system_prompt_append=f"{SLACK_FORMATTING_PROMPT}\n\n{WORKSPACE_PROMPT}",
        mcp_servers=mcp_servers,
        env=env,
        # Screenshots/images the agent Reads back arrive as one big base64 JSON
        # message; the SDK's 1MB default rejects them. 64MB by default.
        max_buffer_size=int(os.getenv("CLAUDE_MAX_BUFFER_SIZE", str(64 * 1024 * 1024))),
        # Evict sessions idle longer than this (default 4h) to free their claude
        # subprocesses; 0 disables. The resume id is kept so the next message
        # resumes the conversation.
        idle_timeout_s=float(os.getenv("SESSION_IDLE_TIMEOUT_S", str(4 * 3600))),
        reap_interval_s=float(os.getenv("SESSION_REAP_INTERVAL_S", "300")),
    )


# --- Slack event handling ---------------------------------------------------

_IGNORED_SUBTYPES = {"bot_message", "message_changed", "message_deleted", "channel_join"}
_MENTION_RE = None  # compiled lazily once we know the bot user id is not needed

_bot_user_id: Optional[str] = None
_bot_user_id_lock = asyncio.Lock()


async def get_bot_user_id(slack: AsyncWebClient) -> Optional[str]:
    """The bot's own Slack user id, cached. Used to skip channel replies that
    re-@mention the bot (the app_mention event handles those). Returns None if
    it can't be resolved yet — callers must tolerate that."""
    global _bot_user_id
    if _bot_user_id is None:
        async with _bot_user_id_lock:
            if _bot_user_id is None:
                try:
                    auth = await slack.auth_test()
                    _bot_user_id = auth.get("user_id")
                except Exception:  # noqa: BLE001
                    log.warning("auth_test failed; can't resolve bot user id yet", exc_info=True)
    return _bot_user_id


# Threads we've confirmed the bot posted in, so repeat lookups are free. Only
# "yes" results are cached (a "no" may become "yes" once the bot is @-mentioned).
_bot_thread_cache: set[str] = set()


async def bot_participates_in_thread(
    slack: AsyncWebClient, channel: str, thread_ts: str, bot_user_id: Optional[str]
) -> bool:
    """True iff the bot has at least one message in this thread (via
    conversations.replies). Last-resort signal for waking on a thread reply
    when we have no local session record — e.g. after a restart that lost the
    session store. Cached per (channel, thread_ts)."""
    if not bot_user_id:
        return False
    key = f"{channel}:{thread_ts}"
    if key in _bot_thread_cache:
        return True
    try:
        resp = await slack.conversations_replies(channel=channel, ts=thread_ts, limit=200)
    except Exception:  # noqa: BLE001 — never let a lookup failure kill the turn
        log.warning("conversations.replies failed for %s", key, exc_info=True)
        return False
    for m in resp.get("messages", []):
        if m.get("user") == bot_user_id:
            _bot_thread_cache.add(key)
            return True
    return False


def normalize_slack_event(
    event: dict[str, Any], bot_user_id: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Turn a raw Slack event into the message payload the handler wants, or
    None if we should ignore it (bot echoes, edits, non-addressed channel chatter).

    We respond to:
      - app_mention events (opening turn / re-summoning the bot),
      - direct-message ('im') messages,
      - replies typed into a channel/group thread the bot is already engaged in
        (no re-@mention needed). Those come as plain `message` events; we tag
        them `needs_active_session` so the caller only wakes on threads with a
        live session.
    """
    if not isinstance(event, dict) or event.get("bot_id"):
        return None
    if event.get("subtype") in _IGNORED_SUBTYPES:
        return None

    etype = event.get("type")
    channel_type = event.get("channel_type")
    needs_active_session = False
    if etype == "app_mention":
        pass
    elif etype == "message" and channel_type == "im":
        pass
    elif (
        etype == "message"
        and channel_type in ("channel", "group", "mpim")
        and event.get("thread_ts")
    ):
        # A reply inside a channel/private thread. If it re-@mentions the bot,
        # the app_mention event covers it — skip here to avoid double-handling.
        if bot_user_id and f"<@{bot_user_id}>" in (event.get("text") or ""):
            return None
        needs_active_session = True
    else:
        return None

    channel = event.get("channel")
    ts = event.get("ts")
    if not channel or not ts:
        return None
    thread_ts = event.get("thread_ts") or ts
    text = event.get("text") or ""
    # Strip a leading bot @-mention ("<@U123> do x" -> "do x").
    text = re.sub(r"^\s*<@[UW][A-Z0-9]+>\s*", "", text).strip()

    return {
        "thread_key": f"{channel}:{thread_ts}",
        "channel": channel,
        "reply_thread_ts": thread_ts,
        "text": text,
        "files": event.get("files") or [],
        "needs_active_session": needs_active_session,
    }


async def handle_user_message(
    payload: dict[str, Any], sessions: SessionManager, slack: AsyncWebClient
) -> None:
    thread_key = payload.get("thread_key")
    channel = payload.get("channel")
    reply_ts = payload.get("reply_thread_ts")
    text = payload.get("text") or ""
    files = payload.get("files") or []
    if not thread_key or not channel or (not text and not files):
        log.warning("skipping message missing thread_key/channel/text")
        return

    if files:
        local = await download_slack_files(files, slack.token or "")
        if local:
            listing = "\n".join(f"- {p}" for p in local)
            text = (f"{text}\n\n" if text else "") + (
                f"The user attached these files (local paths, read them as needed):\n{listing}"
            )

    log.info("message thread=%s channel=%s len=%d files=%d", thread_key, channel, len(text), len(files))
    session = await sessions.get_or_create(thread_key)
    # If a turn on this thread is already running, ours will queue behind it on
    # the session lock — tell the user rather than showing a frozen "thinking…".
    renderer = SlackRenderer(slack, channel, reply_ts, queued=session.is_busy)
    await renderer.open()
    full_text: list[str] = []
    # Whether any of the *thread's* agent's own words reached the message. It
    # gates the terminal render: the user wrote to us, so a turn that ends with
    # nothing on screen must say so rather than leave the "thinking…"
    # placeholder, or a finished tool's label, up forever.
    rendered = False
    try:
        async for chunk in session.send(text):
            if chunk.kind == "turn_start":
                await renderer.mark_active()
            elif chunk.subagent:
                # Another agent's narration, arriving on the shared stream from a
                # sub-agent an earlier turn spawned. Rendering it here would put
                # it in this message as if the thread's agent had said it.
                continue
            elif chunk.kind == "text":
                # `full_text` keeps the ATTACH directives — the upload scan and
                # the email reply below both read it — and loses the sentinel, so
                # the token cannot ride out to an email either.
                stripped = strip_flush_sentinel(chunk.text)
                visible = ATTACH_RE.sub("", stripped)
                if not visible.strip():
                    if stripped.strip():
                        full_text.append(stripped)  # an ATTACH line and nothing else
                    continue
                # Set from what actually reaches Slack, not from what survived the
                # sentinel strip: a reply that is only an ATTACH line renders no
                # text, and counting it here would leave the placeholder standing.
                rendered = True
                full_text.append(stripped)
                await renderer.append(visible)
            elif chunk.kind == "tool_use":
                await renderer.status(tool_label(chunk.name, chunk.args))
        if rendered:
            # The turn is over, so nothing is still running: retire any trailing
            # tool label before the last draw, or a finished reply ends on a line
            # that reads as a tool still in flight.
            renderer.clear_status()
            await renderer.flush(force=True)
        else:
            # Nothing of the agent's own reached Slack — the reply was only the
            # sentinel, only a sub-agent's narration, only an ATTACH line, or only
            # tool calls. A flush turn in this position stays silent, but here the
            # user did write to us, so end on something terminal and true instead
            # of a "thinking…" placeholder that never resolves.
            log.info("nothing rendered for %s; closing the message", thread_key)
            if ATTACH_RE.search("\n".join(full_text)):
                await renderer.replace_with("_see attached_")
            else:
                await renderer.replace_with("_nothing new to report_")
    except Exception as e:  # noqa: BLE001 — surface any turn failure to Slack
        log.exception("session error on %s", thread_key)
        await renderer.replace_with(f":warning: error: `{e}`")

    joined = "\n".join(full_text)
    paths = [m.group(1).strip() for m in ATTACH_RE.finditer(joined)]
    if paths:
        await upload_files(slack, channel, reply_ts, paths)

    # For an email-triggered turn, send Claude's composed reply by email. The
    # Mailgun key lives only in this payload context — never in the Claude turn.
    email_ctx = payload.get("_email")
    if email_ctx:
        await _send_email_reply(email_ctx, joined, channel, reply_ts, slack)


# --- Background-task flush --------------------------------------------------

# The Agent SDK is strictly turn-based: once a turn's result arrives, nothing
# can reach Slack until the next message. A background sub-agent that outlives
# its turn would finish silently — its results only surfacing whenever the user
# happens to write again. The SubagentStop hook (wired per session in
# SessionManager) fires even while a session is idle; we answer it by running a
# synthetic "flush" turn in which the agent sees the pending task notifications
# and reports the finished work to the thread.

# Wait after a SubagentStop before flushing, so a burst of sub-agents finishing
# together is reported by one turn instead of several.
FLUSH_COALESCE_S = 5.0

FLUSH_SENTINEL = "NOTHING_TO_REPORT"

FLUSH_PROMPT = (
    "[Automated wake-up — not a user message. One or more background tasks or "
    "sub-agents in this thread just finished. Report their results to the "
    "thread now, exactly as you would have if you had been waiting for them. "
    "If every finished task's outcome has already been reported and there is "
    f"nothing new to say, reply with exactly {FLUSH_SENTINEL}.]"
)

# A line that is nothing but the sentinel, allowing for the markdown decoration
# the agent reaches for on a short standalone line (the system prompt above tells
# it to italicise with underscores) and a trailing full stop. `re.escape` so a
# future sentinel containing regex metacharacters cannot turn into a wildcard.
_SENTINEL_LINE_RE = re.compile(
    rf"^[ \t]*[*_`~]{{0,3}}{re.escape(FLUSH_SENTINEL)}[*_`~]{{0,3}}[ \t]*[.!]?[ \t]*$\n?",
    re.MULTILINE,
)


def strip_flush_sentinel(text: str) -> str:
    """Remove the flush sentinel from a chunk of agent text.

    The sentinel is protocol between the wrapper and the agent — a reader should
    never see it. Suppressing it only where it is expected is not enough,
    because it reaches the thread three ways:

      1. as the whole reply to a flush turn (the case it was designed for);
      2. as a trailing text block, after the agent has already narrated and run
         tools in a flush turn and only then decides there is nothing new — by
         which point the message is open and a first-chunk check no longer
         applies;
      3. as the whole reply to a *user* message, with no flush involved. The
         convention is in the thread's context, so a content-free message like
         "." reads as "nothing new to say" and the agent answers accordingly.
         Observed in #bizzy-bot on 2026-07-29: three bare NOTHING_TO_REPORT
         posts, turns 26-28, each answering a "." with 15 output tokens.

    Matched a whole line at a time rather than as a bare substring. A substring
    match cuts the token out of text that is *about* it — and this agent's
    workspace is its own repository, so a reply quoting the assignment in this
    very file is one it has to be able to write. Such a reply also rides out to
    the email path via `full_text`, where a silent deletion is invisible.

    Line matching covers both real routes above: one text block is one chunk (see
    `Session.send`), so a sentinel the agent emits as its own block arrives as its
    own chunk, whole. The cost is that a sentinel glued into the middle of a
    sentence still shows — a leaked token reads worse than it looks, but quietly
    editing a real answer is worse still.
    """
    return _SENTINEL_LINE_RE.sub("", text)


async def flush_background_results(
    thread_key: str,
    sessions: SessionManager,
    slack: AsyncWebClient,
    on_turn_start: Optional[Callable[[], None]] = None,
) -> None:
    """Run a synthetic turn that reports finished background work to the thread.

    Posts nothing at all when the agent answers with the sentinel (already
    reported, or the notification landed in an intervening turn), so redundant
    flushes stay invisible on Slack. `on_turn_start` fires once the turn holds
    the session lock, which is the point after which a sub-agent that stops can
    no longer appear in this turn's output."""
    session = sessions.get(thread_key)
    if session is None:
        return  # cleared/reaped since the hook fired — don't resurrect it
    channel, _, reply_ts = thread_key.partition(":")
    log.info("flush: reporting background results on %s", thread_key)
    renderer = SlackRenderer(slack, channel, reply_ts)
    opened = False  # renderer.open() deferred until there's something to say
    full_text: list[str] = []
    error: Optional[str] = None
    try:
        async for chunk in session.send(FLUSH_PROMPT):
            if chunk.kind == "turn_start":
                if on_turn_start is not None:
                    on_turn_start()
            elif chunk.subagent:
                continue  # a sub-agent narrating, not this turn's report
            elif chunk.kind == "text":
                # Strip on every chunk, not just the first: once `opened` is
                # True a guard on the opening text no longer runs, and the agent
                # decides it has nothing to report *after* narrating and running
                # tools, so the sentinel usually arrives once the message is
                # already open.
                clean = strip_flush_sentinel(chunk.text)
                if not clean.strip():
                    continue  # empty, or the sentinel and nothing else
                if not opened:
                    # open() reports whether it posted; if Slack refused, leave
                    # `opened` False so the error path below still tries — going
                    # quiet here would strand the finished sub-agent for good.
                    opened = await renderer.open()
                full_text.append(clean)
                await renderer.append(ATTACH_RE.sub("", clean))
            elif chunk.kind == "tool_use" and opened:
                await renderer.status(tool_label(chunk.name, chunk.args))
            elif chunk.kind == "result" and chunk.is_error:
                # A failed turn arrives as a result chunk rather than an
                # exception, so without this it streams no text and is
                # indistinguishable from the sentinel.
                error = chunk.text
    except Exception as e:  # noqa: BLE001
        log.exception("flush turn failed on %s", thread_key)
        error = str(e)

    if error is not None:
        # Never go quiet on a failed flush: the sub-agent has already stopped,
        # so no further hook fires for it and nothing else would ever report it.
        log.warning("flush: turn failed on %s: %s", thread_key, error)
        if not opened:
            await renderer.open()
        await renderer.replace_with(
            f":warning: a background task finished but reporting it failed: `{error}`"
        )
        return
    if not opened:
        log.info("flush: nothing to report on %s", thread_key)
        return
    # Same reason as the user path: the turn is done, so no tool is still running
    # and the report must not end on a status line. `append()` is the only thing
    # that normally retires one, and the last text chunk here may well have been
    # dropped as the sentinel or as a sub-agent's.
    renderer.clear_status()
    await renderer.flush(force=True)

    paths = [m.group(1).strip() for m in ATTACH_RE.finditer("\n".join(full_text))]
    if paths:
        await upload_files(slack, channel, reply_ts, paths)


async def _send_email_reply(
    ctx: dict, turn_text: str, channel: str, reply_ts: str, slack: AsyncWebClient
) -> None:
    """Extract Claude's reply block from the turn and send it via Mailgun,
    posting a short outcome note to the Slack thread."""
    body = email_reply.extract_reply(turn_text)
    if not body:
        await slack.chat_postMessage(
            channel=channel, thread_ts=reply_ts,
            text=":information_source: No reply sent (no reply block in the response).",
        )
        return
    ok, detail = await email_reply.send_reply(
        ctx.get("mailgun") or {},
        to_addr=ctx.get("from_addr") or "",
        from_addr=ctx.get("recipient") or "",
        subject=ctx.get("subject") or "",
        body=body,
        in_reply_to=ctx.get("message_id"),
    )
    if ok:
        await slack.chat_postMessage(
            channel=channel, thread_ts=reply_ts,
            text=f":white_check_mark: Email reply sent to {ctx.get('from_addr')}.",
        )
    else:
        log.warning("email reply send failed: %s", detail)
        await slack.chat_postMessage(
            channel=channel, thread_ts=reply_ts,
            text=f":warning: Couldn't send the email reply: `{detail}`",
        )


async def handle_email_event(
    payload: dict[str, Any], sessions: SessionManager, slack: AsyncWebClient
) -> None:
    """An inbound email routed to this agent by Central (see docs/EMAIL.md):
    announce it in the configured channel, then run a Claude turn (in that
    thread) to compose a reply, which is sent by email afterward."""
    channel = payload.get("channel")
    if not channel:
        log.warning("email event missing channel; dropping %s", payload.get("message_id"))
        return
    sender = payload.get("from_raw") or payload.get("from_addr") or "unknown sender"
    subject = payload.get("subject") or "(no subject)"
    body = payload.get("body") or ""

    # Escaped: sender, subject and body are attacker-controlled, and Slack would
    # otherwise render `<http://evil.example|IT: reset your password>` in the
    # announce message as a real link.
    announce = f":email: *New email* from {_slack_escape(sender)}\n> *{_slack_escape(subject)}*"
    if body:
        snippet = " ".join(body.split())
        if len(snippet) > 200:
            snippet = snippet[:199] + "…"
        announce += f"\n> {_slack_escape(snippet)}"
    try:
        resp = await slack.chat_postMessage(channel=channel, text=announce)
    except Exception:  # noqa: BLE001
        log.exception("email announce failed for %s", payload.get("message_id"))
        return
    parent_ts = resp["ts"]

    synth = {
        "thread_key": f"{channel}:{parent_ts}",
        "channel": channel,
        "reply_thread_ts": parent_ts,
        "text": email_reply.build_instruction(sender=sender, subject=subject, body=body),
        "files": [],
        # Reply context (incl. the transient Mailgun creds) for the post-turn
        # send. Deliberately NOT part of the prompt text above.
        "_email": {
            "message_id": payload.get("message_id"),
            "from_addr": payload.get("from_addr"),
            "recipient": payload.get("recipient"),
            "subject": subject,
            "mailgun": payload.get("mailgun") or {},
        },
    }
    await handle_user_message(synth, sessions, slack)


async def handle_pr_review_request(
    pr: pr_poller.PR, channel: str, sessions: SessionManager, slack: AsyncWebClient
) -> None:
    """A GitHub PR just named this agent as a requested reviewer: announce it in
    the configured channel, then run a Claude turn in that thread which reviews
    the PR and submits the review with `gh pr review`.

    Same shape as handle_email_event, minus the post-turn send step — Claude
    submits the review itself, and `gh` auth is ambient on this machine, so
    there are no credentials to keep out of the prompt.
    """
    link = f"<{pr.url}|{pr.repo}#{pr.number}>"
    draft = " _(draft)_" if pr.is_draft else ""
    announce = f":eyes: *Review requested* — {link}{draft}\n> *{_slack_escape(pr.title)}*"
    try:
        resp = await slack.chat_postMessage(channel=channel, text=announce)
    except Exception as exc:  # noqa: BLE001 — contained by the poll loop
        log.exception(
            "PR announce failed for %s in channel %s — is PR_REVIEW_CHANNEL a channel "
            "id the bot has been invited to?", pr.key, channel,
        )
        # No Claude turn (and therefore no GitHub write) has started yet. Tell
        # the poller this specific failure is safe to release and retry rather
        # than silently suppressing the request for the life of the process.
        raise pr_poller.ReviewNotStarted from exc
    parent_ts = resp["ts"]

    synth = {
        "thread_key": f"{channel}:{parent_ts}",
        "channel": channel,
        "reply_thread_ts": parent_ts,
        "text": pr_poller.build_review_instruction(pr),
        "files": [],
    }
    await handle_user_message(synth, sessions, slack)


async def handle_sentry_issue(
    group: sentry_poller.IssueGroup,
    channel: str,
    sessions: SessionManager,
    slack: AsyncWebClient,
    parent_ts: Optional[str] = None,
) -> None:
    """A new production Sentry issue appeared: announce it in the configured
    channel, then run a Claude turn in that thread which triages it via the
    sentry-triage skill (investigation, and Jira filing when proven).

    `parent_ts` threads the announce under a storm rollup message instead of
    posting a fresh channel message.
    """
    p = group.primary
    link = f"<{p.permalink}|{p.short_id}>"
    siblings = "".join(
        f" + <{s.permalink}|{s.short_id}>" for s in group.siblings
    )
    announce = (
        f":rotating_light: *New Sentry issue* — {link}{siblings} ({p.project_slug})\n"
        f"> *{_slack_escape(p.title)}*\n"
        f"> {p.count} event(s), {p.user_count} user(s) — `{_slack_escape(p.culprit)}`"
    )
    try:
        resp = await slack.chat_postMessage(
            channel=channel, thread_ts=parent_ts, text=announce
        )
    except Exception as exc:  # noqa: BLE001 — contained by the poll loop
        log.exception(
            "Sentry announce failed for %s in channel %s — is SENTRY_WATCH_CHANNEL a "
            "channel id the bot has been invited to?", p.short_id, channel,
        )
        # No Claude turn started; the poller may safely release its claim.
        raise sentry_poller.TriageNotStarted from exc
    thread_ts = parent_ts or resp["ts"]

    synth = {
        "thread_key": f"{channel}:{thread_ts}",
        "channel": channel,
        "reply_thread_ts": thread_ts,
        "text": sentry_poller.build_triage_instruction(group),
        "files": [],
    }
    await handle_user_message(synth, sessions, slack)


async def handle_sentry_rollup(
    triage: list[sentry_poller.IssueGroup],
    listed: list[sentry_poller.IssueGroup],
    channel: str,
    slack: AsyncWebClient,
) -> None:
    """A storm (many new issues in one poll): one summary message for the whole
    batch. The poller then auto-triages `triage` in their own threads; `listed`
    stays a manual checklist here."""
    def _line(g: sentry_poller.IssueGroup) -> str:
        p = g.primary
        extra = f" (+{len(g.siblings)} sibling)" if g.siblings else ""
        return (
            f"• <{p.permalink}|{p.short_id}>{extra} — {_slack_escape(p.title)} "
            f"({p.count} ev / {p.user_count} usr)"
        )

    lines = [
        f":rotating_light: *Sentry storm: {len(triage) + len(listed)} new issue "
        f"group(s) in one poll.* Auto-triaging the top {len(triage)}:",
        *(_line(g) for g in triage),
    ]
    if listed:
        lines += [
            f"Not auto-triaged ({len(listed)}) — ask me to `triage <id>` for any of these:",
            *(_line(g) for g in listed),
        ]
    await slack.chat_postMessage(channel=channel, text="\n".join(lines))


async def handle_clear(payload: dict, sessions: SessionManager, slack: AsyncWebClient) -> None:
    channel, reply_ts, thread_key = payload.get("channel"), payload.get("reply_thread_ts"), payload.get("thread_key")
    if not thread_key or not channel:
        return
    cleared = await sessions.drop(thread_key)
    await slack.chat_postMessage(
        channel=channel, thread_ts=reply_ts,
        text=":wastebasket: session cleared — the next message starts fresh." if cleared else "_No active session to clear._",
    )


async def handle_stop(payload: dict, sessions: SessionManager, slack: AsyncWebClient) -> None:
    channel, reply_ts, thread_key = payload.get("channel"), payload.get("reply_thread_ts"), payload.get("thread_key")
    if not thread_key or not channel:
        return
    session = sessions.get(thread_key)
    stopped = await session.interrupt() if session else False
    await slack.chat_postMessage(
        channel=channel, thread_ts=reply_ts,
        text=":octagonal_sign: stopped." if stopped else "_Nothing running._",
    )


async def handle_help(payload: dict, sessions: SessionManager, slack: AsyncWebClient) -> None:
    channel, reply_ts = payload.get("channel"), payload.get("reply_thread_ts")
    if not channel:
        return
    lines = "\n".join(f"• `{cmd}` — {desc}" for cmd, (_, desc) in META_COMMANDS.items())
    await slack.chat_postMessage(channel=channel, thread_ts=reply_ts, text=f":question: *Commands*\n{lines}")


META_COMMANDS: dict[str, tuple[Callable[..., Awaitable[None]], str]] = {
    "!stop": (handle_stop, "interrupt the turn currently running"),
    "!clear": (handle_clear, "reset this thread's session"),
    "!help": (handle_help, "show this list"),
}


async def dispatch_event(payload: Any, sessions: SessionManager, slack: AsyncWebClient) -> None:
    """Handle one Slack event delivered by Central-Dispatch."""
    bot_user_id = await get_bot_user_id(slack)
    msg = normalize_slack_event(payload, bot_user_id)
    if msg is None:
        return  # not addressed to us / an echo — ignore (still acked)
    # A bare channel-thread reply only wakes the bot if it's already engaged in
    # that thread — otherwise any reply in any channel the bot sits in would
    # trigger it. "Engaged" is, cheapest first: a live in-memory session, a
    # persisted resume id (survives a restart), or — last resort — the bot
    # actually appearing in the thread per conversations.replies.
    if msg.pop("needs_active_session", False):
        thread_key = msg["thread_key"]
        engaged = (
            sessions.exists(thread_key)
            or sessions.has_resume(thread_key)
            or await bot_participates_in_thread(
                slack, msg["channel"], msg["reply_thread_ts"], bot_user_id
            )
        )
        if not engaged:
            return
    meta = META_COMMANDS.get((msg.get("text") or "").strip().lower())
    if meta:
        await meta[0](msg, sessions, slack)
    else:
        await handle_user_message(msg, sessions, slack)


# --- Delivery cursor --------------------------------------------------------


class Cursor:
    """Tracks the highest *contiguous* sequence number fully processed, so we ack
    (and resume from) exactly that point even when events finish out of order.

    Where we start counting is only ever a guess: our persisted position, or 0 if
    that state was never written or was lost (a reinstall, a wiped state dir).
    Central-Dispatch numbers events from a per-agent counter that has been
    climbing since registration, so the guess can sit far below the first seq we
    are actually sent — and then "contiguous" waits forever for a seq that will
    never arrive: nothing is ever acked, Central-Dispatch never prunes its log,
    and every reconnect replays the whole backlog into fresh Claude turns.
    `observe` pins the baseline to what the server really sends instead."""

    def __init__(self, last: int):
        self.contiguous = last
        self._done: set[int] = set()
        self._based = False

    def observe(self, seq: int) -> Optional[int]:
        """Adopt the first seq delivered on a connection as the baseline, before
        it is processed. Events arrive in ascending seq order (replay is ordered,
        live pushes follow it), so the first one lower-bounds everything we could
        still be sent: anything below it the server has already accounted for —
        acked and pruned, or dropped by its staleness failsafe — so waiting on it
        would wedge us forever. Only ever moves the cursor forward. Returns the
        new baseline if it jumped, else None (for logging)."""
        if self._based:
            return None
        self._based = True
        if seq - 1 > self.contiguous:
            self.contiguous = seq - 1
            return self.contiguous
        return None

    def complete(self, seq: int) -> Optional[int]:
        """Mark seq done. Return the new contiguous high-water mark if it moved."""
        self._done.add(seq)
        moved = False
        while (self.contiguous + 1) in self._done:
            self.contiguous += 1
            self._done.discard(self.contiguous)
            moved = True
        return self.contiguous if moved else None


def _load_last_seq() -> int:
    try:
        with open(STATE_PATH) as f:
            return int(json.load(f).get("last_seq", 0))
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _save_last_seq(seq: int) -> None:
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"last_seq": seq}, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log.warning("could not persist agent-wrapper state: %s", e)


# --- WebSocket consumer -----------------------------------------------------


async def consume(
    http: aiohttp.ClientSession,
    ws_url: str,
    ws_token: str,
    on_event: Callable[[Any], Awaitable[None]],
    stop: asyncio.Event,
) -> None:
    """Connect to Central-Dispatch's WebSocket and process events until `stop` is set.
    Reconnects with backoff, resuming from the last contiguous seq we acked."""
    backoff = 1.0
    while not stop.is_set():
        url = f"{ws_url}?token={ws_token}&lastSeq={_load_last_seq()}"
        try:
            async with http.ws_connect(url, heartbeat=30, ssl=_insecure_tls_ctx(url)) as ws:
                log.info("connected to Central-Dispatch (lastSeq=%d)", _load_last_seq())
                backoff = 1.0
                cursor = Cursor(_load_last_seq())
                send_lock = asyncio.Lock()
                inflight: set[asyncio.Task] = set()

                # Close the socket when asked to stop, so the receive loop below
                # unblocks promptly (a plain `async for` would hang until the next
                # frame arrives).
                async def _closer() -> None:
                    await stop.wait()
                    await ws.close()

                closer = asyncio.create_task(_closer())

                async def process(seq: int, event: Any) -> None:
                    try:
                        await on_event(event)
                    except Exception:  # noqa: BLE001 — one bad event mustn't kill the loop
                        log.exception("event handler failed (seq=%d)", seq)
                    moved = cursor.complete(seq)
                    if moved is not None:
                        _save_last_seq(moved)
                        async with send_lock:
                            if not ws.closed:
                                await ws.send_str(json.dumps({"type": "ack", "seq": moved}))

                try:
                    async for raw in ws:
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            data = json.loads(raw.data)
                        except ValueError:
                            continue
                        if data.get("type") == "event" and isinstance(data.get("seq"), int):
                            # Baseline before processing, so the first event's own
                            # ack can move the cursor (and get persisted).
                            rebased = cursor.observe(data["seq"])
                            if rebased is not None:
                                log.info(
                                    "delivery cursor rebased to %d (local state was behind "
                                    "Central-Dispatch's log)",
                                    rebased,
                                )
                            t = asyncio.create_task(
                                process(data["seq"], data.get("event") or {})
                            )
                            inflight.add(t)
                            t.add_done_callback(inflight.discard)
                finally:
                    closer.cancel()

                # Socket closed — let in-flight turns finish before reconnecting.
                if inflight:
                    await asyncio.gather(*inflight, return_exceptions=True)
        except aiohttp.ClientError as e:
            # aiohttp formats ClientResponseError with the full request URL, and
            # ours carries ?token=… — so logging `e` raw would write a live
            # Central-Dispatch credential into this run's log file, on every
            # retry. Ephemeral on stderr; not in a file that is never pruned and
            # sits in the same home dir as a bypassPermissions agent.
            detail = str(e).replace(ws_token, "***") if ws_token else str(e)
            log.warning("Central-Dispatch connection error: %s", detail)

        if stop.is_set():
            return
        log.info("reconnecting in %.0fs", backoff)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            return
        except asyncio.TimeoutError:
            backoff = min(backoff * 2, 30.0)


def pr_poller_config(settings: dict[str, str]) -> tuple[Optional[str], str, float]:
    """(channel, login, interval) for the PR review poller. A None channel means
    the feature is off. Values come from the real environment first, then
    ~/.bizzybot/settings.env.

    PR_REVIEW_LOGIN defaults to `@me`, which `gh` resolves to whoever the local
    CLI is authenticated as — on an agent machine that is the bot account, so the
    common case needs no configuration. An explicit login or `@org/team` also works.
    """
    channel = resolve(settings, "PR_REVIEW_CHANNEL")
    login = resolve(settings, "PR_REVIEW_LOGIN") or "@me"
    raw = resolve(settings, "PR_POLL_INTERVAL_S")
    try:
        interval = float(raw) if raw else 60.0
    except ValueError:
        log.warning("PR_POLL_INTERVAL_S=%r is not a number; using 60", raw)
        interval = 60.0
    return channel, login, interval


def sentry_poller_config(settings: dict[str, str]) -> tuple[Optional[str], str, str, float]:
    """(channel, token, org, interval) for the Sentry watch poller. A None
    channel means the feature is off — the same "unconfigured means no-op"
    shape as the PR poller. A channel with no token is a configuration mistake,
    so it warns rather than silently doing nothing forever."""
    channel = resolve(settings, "SENTRY_WATCH_CHANNEL")
    token = resolve(settings, "SENTRY_API_TOKEN") or ""
    org = resolve(settings, "SENTRY_ORG") or "biztrip-ai"
    raw = resolve(settings, "SENTRY_POLL_INTERVAL_S")
    try:
        interval = float(raw) if raw else sentry_poller.DEFAULT_INTERVAL_S
    except ValueError:
        log.warning(
            "SENTRY_POLL_INTERVAL_S=%r is not a number; using %.0f",
            raw, sentry_poller.DEFAULT_INTERVAL_S,
        )
        interval = sentry_poller.DEFAULT_INTERVAL_S
    if channel and not token:
        log.warning(
            "SENTRY_WATCH_CHANNEL is set but SENTRY_API_TOKEN is not; "
            "Sentry watch stays disabled"
        )
        channel = None
    return channel, token, org, interval


async def main() -> None:
    central_dispatch = resolve_central_dispatch()
    preflight()

    async with aiohttp.ClientSession() as http:
        reg = None
        while reg is None:
            token, from_env = resolve_token(central_dispatch)
            try:
                reg = await register(http, central_dispatch, token)
            except AuthError as e:
                if from_env:
                    sys.exit(f"REGISTRATION_TOKEN was rejected by Central-Dispatch: {e}")
                _forget_token()
                if not sys.stdin.isatty():
                    sys.exit("Saved registration token was rejected; set a valid one and restart.")
                log.error("that registration token was rejected — let's try another")
                # loop: resolve_token() will prompt again

        slack_token = reg.get("slackBotToken") or os.getenv("SLACK_BOT_TOKEN") or ""
        if not slack_token:
            log.warning("no Slack bot token from Central-Dispatch; replies to Slack will fail until one is configured")
        ws_info = reg.get("ws") or {}
        ws_url, ws_token = ws_info.get("url"), ws_info.get("token")
        if not ws_url or not ws_token:
            sys.exit("Central-Dispatch did not return WebSocket details")

        settings = load_settings()
        sessions = build_session_manager(settings)
        sessions.start_reaper()
        slack = AsyncWebClient(token=slack_token)

        # PR review poller. Off unless PR_REVIEW_CHANNEL is set — the same
        # "unconfigured means no-op" shape as Central's email poller.
        pr_channel, pr_login, pr_interval = pr_poller_config(settings)
        poller: Optional[pr_poller.PRPoller] = None
        if pr_channel:
            async def on_pr_review_requested(pr: pr_poller.PR) -> None:
                await handle_pr_review_request(pr, pr_channel, sessions, slack)

            # Constructed here but started inside the try below, so the finally
            # that stops it covers every path on which it is running.
            poller = pr_poller.PRPoller(
                login=pr_login, on_new=on_pr_review_requested, interval_s=pr_interval
            )
        else:
            log.info("PR review poller disabled (set PR_REVIEW_CHANNEL to enable)")

        # Sentry watch poller — same lifecycle as the PR poller above.
        sw_channel, sw_token, sw_org, sw_interval = sentry_poller_config(settings)
        sentry_watch: Optional[sentry_poller.SentryPoller] = None
        if sw_channel:
            async def on_sentry_issue(group: sentry_poller.IssueGroup) -> None:
                await handle_sentry_issue(group, sw_channel, sessions, slack)

            async def on_sentry_rollup(
                triage: list[sentry_poller.IssueGroup],
                listed: list[sentry_poller.IssueGroup],
            ) -> None:
                await handle_sentry_rollup(triage, listed, sw_channel, slack)

            sentry_watch = sentry_poller.SentryPoller(
                token=sw_token, org=sw_org,
                on_new=on_sentry_issue, on_rollup=on_sentry_rollup,
                interval_s=sw_interval,
            )
        else:
            log.info("Sentry watch disabled (set SENTRY_WATCH_CHANNEL to enable)")

        # Wake threads when a background sub-agent finishes (see the
        # "Background-task flush" section above). thread_key -> the task holding
        # that thread's coalescing window, so a window that closes never clears
        # a newer one.
        flush_pending: dict[str, asyncio.Task] = {}
        flush_tasks: set[asyncio.Task] = set()

        def on_subagent_stop(thread_key: str) -> None:
            # Deliberately no `is_busy` check. A sub-agent that stops late in a
            # running turn — after the agent already composed its answer — is
            # absent from that turn's output, so skipping it here would strand
            # it for good. Scheduling regardless costs at most one extra turn,
            # which the sentinel keeps invisible.
            if sessions.get(thread_key) is None or thread_key in flush_pending:
                return

            async def _flush_later() -> None:
                try:
                    await asyncio.sleep(FLUSH_COALESCE_S)
                    # The window closes when the turn takes the session lock,
                    # not before: everything that stopped up to that point is
                    # covered by this turn, and anything stopping after it opens
                    # a fresh window rather than being swallowed.
                    await flush_background_results(
                        thread_key, sessions, slack, on_turn_start=_close_window
                    )
                finally:
                    _close_window()  # cancelled, or the session was reaped

            def _close_window() -> None:
                if flush_pending.get(thread_key) is task:
                    del flush_pending[thread_key]

            task = asyncio.create_task(_flush_later())
            flush_pending[thread_key] = task
            flush_tasks.add(task)
            task.add_done_callback(flush_tasks.discard)

        sessions.on_subagent_stop = on_subagent_stop

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async def on_event(event: Any) -> None:
            # Central wraps each event as {type, payload}. Slack events go through
            # the normal dispatch; 'email' events (see docs/EMAIL.md) are handled
            # directly. Default to slack for anything untyped (back-compat).
            event = event or {}
            etype = event.get("type")
            payload = event.get("payload")
            if etype == "email":
                await handle_email_event(payload, sessions, slack)
            else:
                await dispatch_event(payload, sessions, slack)

        try:
            if poller is not None:
                poller.start()
            if sentry_watch is not None:
                sentry_watch.start()
            await consume(http, ws_url, ws_token, on_event, stop)
        finally:
            log.info("shutting down")
            # Stop the poller before the sessions: the other order lets a review
            # turn keep streaming into a session whose subprocess was just
            # killed, which surfaces as a spurious error in Slack on every exit.
            if poller is not None:
                await poller.stop()
            if sentry_watch is not None:
                await sentry_watch.stop()
            await sessions.close_all()


def _setup_logging() -> Optional[str]:
    """Log to stderr *and* to a fresh file for this run; return the file's path.

    Called once from main_sync — not at import time, so merely importing the
    package doesn't litter log files. Every logger in the package is left
    unconfigured and propagates to the root, so this one call covers all of them.

    The file is named for the moment the run started plus our pid —
    ``agent-wrapper-20260729-153012-4711.log``, under ``~/.bizzybot/logs/`` — so
    it sorts chronologically, and two runs sharing a state dir can't land on the
    same name and shred each other's records (the timestamp alone has one-second
    resolution, and repeats outright across a DST fall-back).

    Each file is capped at BIZZYBOT_LOG_MAX_BYTES (1 MiB). backupCount has to be
    at least 1: with 0, RotatingFileHandler's rollover reopens the same file in
    append mode and the cap does nothing at all. So a run that outgrows the cap
    keeps its most recent megabyte in the ``.log``, the megabyte before that in
    ``.log.1``, and drops anything older.

    The file is best-effort: an unwritable logs dir or an unparseable
    BIZZYBOT_LOG_MAX_BYTES leaves us console-only, rather than stopping an agent
    whose whole job is to stay connected. Returns None in that case.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path: Optional[str] = None
    problem: Optional[str] = None
    try:
        name = time.strftime("agent-wrapper-%Y%m%d-%H%M%S") + f"-{os.getpid()}.log"
        path = log_path(name)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=int(os.getenv("BIZZYBOT_LOG_MAX_BYTES", str(1024 * 1024))),
                backupCount=1,
                # Explicit utf-8 rather than the locale's encoding: on a non-UTF-8
                # locale (LC_ALL=en_US.ISO8859-1, or a Windows code page) the
                # default would raise inside emit() and silently drop every record
                # carrying an emoji — and we log Slack message text. `errors` for
                # the reason sys.stderr sets it too: a filename that isn't valid
                # UTF-8 reaches us as lone surrogates, which utf-8 can't encode
                # either, and that would drop the record just the same.
                encoding="utf-8",
                errors="backslashreplace",
            )
        )
    except (OSError, ValueError) as e:
        path, problem = None, str(e)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    if problem:
        log.warning("no log file, logging to console only: %s", problem)
    return path


def _prevent_sleep_on_macos() -> None:
    """Keep the Mac awake while the wrapper runs so an idle laptop doesn't sleep
    and drop the WebSocket to Central-Dispatch.

    Spawns `caffeinate -i -w <pid>` as a detached child: `-i` prevents idle system
    sleep, `-s` also prevents system sleep on AC power, and `-w <pid>` ties it to
    our process — caffeinate exits on its own when we exit, so there's nothing to
    clean up and no orphan. No-op off macOS, if caffeinate isn't found, or if
    BIZZYBOT_NO_CAFFEINATE is set. Note caffeinate can't override lid-close sleep
    on battery — for that, use a launchd agent + pmset.
    """
    if sys.platform != "darwin" or os.getenv("BIZZYBOT_NO_CAFFEINATE"):
        return
    caffeinate = shutil.which("caffeinate")
    if not caffeinate:
        log.warning("caffeinate not found; the Mac may sleep and drop the connection")
        return
    try:
        subprocess.Popen(
            [caffeinate, "-i", "-s", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("caffeinate: preventing idle sleep while bizzybot runs")
    except Exception as e:  # never let keep-awake stop the wrapper from starting
        log.warning("could not start caffeinate (%s); the Mac may sleep", e)


def main_sync() -> None:
    """Console-script entry point (`bizzybot`). Sync wrapper around main()."""
    log_file = _setup_logging()
    if log_file:
        log.info("logging to %s", log_file)
    _prevent_sleep_on_macos()
    # Why the exit reason is logged and not just raised: an uncaught traceback
    # (or sys.exit's message) is written straight to stderr by the interpreter,
    # bypassing logging — so it would be missing from the very file you'd go
    # read to find out why the agent stopped.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
        # Exit 130 (128 + SIGINT) the way a Ctrl-C'd process is expected to, which
        # is what a bare `raise` here would have given us — just without dumping a
        # KeyboardInterrupt traceback for what is a normal way to stop the agent.
        sys.exit(130)
    except SystemExit as e:
        if e.code not in (0, None):
            log.error("exiting: %s", e.code)
        raise
    except Exception:
        log.exception("unhandled error; exiting")
        # log.exception has already put the traceback on stderr *and* in the file,
        # so re-raising would print the whole thing a second time. Exit 1, which is
        # what the interpreter would have given us for an uncaught exception.
        sys.exit(1)


if __name__ == "__main__":
    main_sync()
