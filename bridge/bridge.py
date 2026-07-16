"""Claudebot OSS bridge.

Runs in the agent workspace (a laptop, VM, or container — set up by hand). It:
  1. dials home to Central with a registration token and pulls the Slack bot
     token + WebSocket details (see central/docs, "Registration (dial-home)");
  2. runs a local preflight (Claude Code, gh, git);
  3. opens a WebSocket to Central, replays anything it missed while offline
     (via lastSeq), then processes live Slack events;
  4. drives one persistent Claude Code session per Slack thread and posts the
     replies straight back to Slack.

No Ably, no cloud provisioning, no idle keep-alive. Uses your own local git/gh
auth. Run:  uv run python bridge.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from typing import Any, Awaitable, Callable, Optional

import ssl

import aiohttp
from dotenv import load_dotenv
from slack_sdk.web.async_client import AsyncWebClient

from session_manager import SessionManager, load_cli_mcp_servers
from slack_io import SlackRenderer, download_slack_files, upload_files, tool_label, ATTACH_RE

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bridge")

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(STATE_DIR, ".bridge-state.json")
CONFIG_PATH = os.path.join(STATE_DIR, ".bridge-config.json")

# Default hosted Central. Override with CENTRAL_URL (env/.env) or the saved config.
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


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _insecure_tls_ctx(url: str):
    """For a local Central over TLS (https/wss to localhost with a self-signed
    cert), return an SSL context that skips verification. Returns None for plain
    http/ws or real remote hosts, which keep normal verification.

    Enable for non-localhost hosts with CLAUDEBOT_INSECURE_TLS=1 if needed.
    """
    if not (url.startswith("https") or url.startswith("wss")):
        return None
    is_local = "localhost" in url or "127.0.0.1" in url
    if not (is_local or _truthy(os.getenv("CLAUDEBOT_INSECURE_TLS"))):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_sources(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


# --- Config / registration --------------------------------------------------


class AuthError(Exception):
    """Central rejected the registration token."""


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
        log.warning("could not save bridge config: %s", e)


def _forget_token() -> None:
    cfg = _load_local_config()
    cfg.pop("registration_token", None)
    _save_local_config(cfg)


def resolve_central() -> str:
    """CENTRAL_URL env > saved config > the default hosted Central."""
    saved = _load_local_config().get("central_url")
    return (os.getenv("CENTRAL_URL") or saved or DEFAULT_CENTRAL_URL).rstrip("/")


def resolve_token(central: str) -> tuple[str, bool]:
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
            "the bridge interactively so it can prompt for one.\n"
            f"Get a token: sign in at {central} and open your workspace dashboard."
        )
    token = _prompt_for_token(central)
    _save_local_config({"registration_token": token, "central_url": central})
    return token, False


def _prompt_for_token(central: str) -> str:
    print("\n  Claudebot bridge — first-time setup")
    print(f"  Central: {central}")
    print(f"  Sign in at {central} and open your dashboard to copy your registration token.\n")
    try:
        token = input("  Paste your registration token: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted")
    if not token:
        sys.exit("no token entered")
    return token


async def register(http: aiohttp.ClientSession, central: str, token: str) -> dict[str, Any]:
    """Dial home: exchange the registration token for the Slack bot token and
    WebSocket connection details. Raises AuthError if the token is rejected."""
    async with http.post(
        f"{central}/api/register", json={"token": token}, ssl=_insecure_tls_ctx(central)
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
        sys.exit("Preflight failed — fix the above and restart the bridge.")


# --- Session manager --------------------------------------------------------


def build_session_manager() -> SessionManager:
    extra_args: dict[str, str | None] = {}
    if _truthy(os.getenv("CLAUDE_CHROME", "0")):
        extra_args["chrome"] = None
    cwd = os.getenv("CLAUDE_CWD") or os.path.expanduser("~")
    mcp_servers = (
        load_cli_mcp_servers(cwd) if _truthy(os.getenv("CLAUDE_LOAD_CLI_MCP", "1")) else {}
    )
    return SessionManager(
        cwd=cwd,
        permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions"),
        model=os.getenv("CLAUDE_MODEL") or None,
        setting_sources=_parse_sources(
            os.getenv("CLAUDE_SETTING_SOURCES", "user,project,local")
        ),
        extra_args=extra_args,
        system_prompt_append=SLACK_FORMATTING_PROMPT,
        mcp_servers=mcp_servers,
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
    import re

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
    renderer = SlackRenderer(slack, channel, reply_ts)
    await renderer.open()
    session = await sessions.get_or_create(thread_key)
    full_text: list[str] = []
    try:
        async for chunk in session.send(text):
            if chunk.kind == "text":
                full_text.append(chunk.text)
                await renderer.append(ATTACH_RE.sub("", chunk.text))
            elif chunk.kind == "tool_use":
                await renderer.status(tool_label(chunk.name, chunk.args))
        await renderer.flush(force=True)
    except Exception as e:  # noqa: BLE001 — surface any turn failure to Slack
        log.exception("session error on %s", thread_key)
        await renderer.replace_with(f":warning: error: `{e}`")

    paths = [m.group(1).strip() for m in ATTACH_RE.finditer("\n".join(full_text))]
    if paths:
        await upload_files(slack, channel, reply_ts, paths)


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
    """Handle one Slack event delivered by Central."""
    bot_user_id = await get_bot_user_id(slack)
    msg = normalize_slack_event(payload, bot_user_id)
    if msg is None:
        return  # not addressed to us / an echo — ignore (still acked)
    # A bare channel-thread reply only wakes the bot if it's already engaged in
    # that thread (a live session). Otherwise any reply in any channel the bot
    # sits in would trigger it. The app_mention path is what first creates the
    # session, so this gate opens naturally after the bot is summoned once.
    if msg.pop("needs_active_session", False) and not sessions.exists(msg["thread_key"]):
        return
    meta = META_COMMANDS.get((msg.get("text") or "").strip().lower())
    if meta:
        await meta[0](msg, sessions, slack)
    else:
        await handle_user_message(msg, sessions, slack)


# --- Delivery cursor --------------------------------------------------------


class Cursor:
    """Tracks the highest *contiguous* sequence number fully processed, so we ack
    (and resume from) exactly that point even when events finish out of order."""

    def __init__(self, last: int):
        self.contiguous = last
        self._done: set[int] = set()

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
        log.warning("could not persist bridge state: %s", e)


# --- WebSocket consumer -----------------------------------------------------


async def consume(
    http: aiohttp.ClientSession,
    ws_url: str,
    ws_token: str,
    on_event: Callable[[Any], Awaitable[None]],
    stop: asyncio.Event,
) -> None:
    """Connect to Central's WebSocket and process events until `stop` is set.
    Reconnects with backoff, resuming from the last contiguous seq we acked."""
    backoff = 1.0
    while not stop.is_set():
        url = f"{ws_url}?token={ws_token}&lastSeq={_load_last_seq()}"
        try:
            async with http.ws_connect(url, heartbeat=30, ssl=_insecure_tls_ctx(url)) as ws:
                log.info("connected to Central (lastSeq=%d)", _load_last_seq())
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
                            t = asyncio.create_task(
                                process(data["seq"], (data.get("event") or {}).get("payload"))
                            )
                            inflight.add(t)
                            t.add_done_callback(inflight.discard)
                finally:
                    closer.cancel()

                # Socket closed — let in-flight turns finish before reconnecting.
                if inflight:
                    await asyncio.gather(*inflight, return_exceptions=True)
        except aiohttp.ClientError as e:
            log.warning("Central connection error: %s", e)

        if stop.is_set():
            return
        log.info("reconnecting in %.0fs", backoff)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            return
        except asyncio.TimeoutError:
            backoff = min(backoff * 2, 30.0)


async def main() -> None:
    central = resolve_central()
    preflight()

    async with aiohttp.ClientSession() as http:
        reg = None
        while reg is None:
            token, from_env = resolve_token(central)
            try:
                reg = await register(http, central, token)
            except AuthError as e:
                if from_env:
                    sys.exit(f"REGISTRATION_TOKEN was rejected by Central: {e}")
                _forget_token()
                if not sys.stdin.isatty():
                    sys.exit("Saved registration token was rejected; set a valid one and restart.")
                log.error("that registration token was rejected — let's try another")
                # loop: resolve_token() will prompt again

        slack_token = reg.get("slackBotToken") or os.getenv("SLACK_BOT_TOKEN") or ""
        if not slack_token:
            log.warning("no Slack bot token from Central; replies to Slack will fail until one is configured")
        ws_info = reg.get("ws") or {}
        ws_url, ws_token = ws_info.get("url"), ws_info.get("token")
        if not ws_url or not ws_token:
            sys.exit("Central did not return WebSocket details")

        sessions = build_session_manager()
        slack = AsyncWebClient(token=slack_token)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async def on_event(payload: Any) -> None:
            await dispatch_event(payload, sessions, slack)

        try:
            await consume(http, ws_url, ws_token, on_event, stop)
        finally:
            log.info("shutting down")
            await sessions.close_all()


if __name__ == "__main__":
    asyncio.run(main())
