"""`!!` — run a shell command straight from Slack, no Claude turn involved.

A message like `!! git status` runs in the agent's working directory and posts
the output back to the thread. Only the agent's **sponsor** (the human
responsible for it — see Central-Dispatch's sponsors section) may do this:
everyone else who can talk to the bot would otherwise have a shell on the
machine it runs on.

The command and its output are also kept for the thread and handed to the agent
with the next message there, the way Claude Code's own `!` prefix works, so
"!! npm test" followed by "fix those failures" reads as one conversation.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import signal
import time
from typing import Optional

log = logging.getLogger("agent-wrapper.shell")

PREFIX = "!!"

# Defaults; each is overridable from the environment (see .env.example).
DEFAULT_TIMEOUT_S = 120.0
# Hard cap on what we keep from a command, so a runaway `yes` can't eat memory.
MAX_CAPTURE_CHARS = 200_000
# What goes into the Slack code block (Slack's own limit is ~3000 per message).
MAX_SLACK_CHARS = 2400
# What the agent sees on its next turn in this thread.
MAX_AGENT_CHARS = 8_000
# Results waiting to be handed to the agent, per thread.
MAX_PENDING_PER_THREAD = 5

# Slack turns a bare URL into <https://x|x> (or <https://x>), an email into
# <mailto:a@b|a@b>, and a channel into <#C123|name>. Unwrap to what was typed.
_ANGLE_RE = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")
# ``` fenced ``` or `inline` code around the whole command.
_FENCE_RE = re.compile(r"^```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```$", re.DOTALL)
_TICKS_RE = re.compile(r"^`(.*)`$", re.DOTALL)
# Slack's "smart" typography, which a shell would choke on.
_SMART = {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "--"}


def _unwrap_angle(m: re.Match[str]) -> str:
    target, label = m.group(1), m.group(2)
    if target.startswith(("@", "#", "!")):
        # A user/channel/special mention: keep the label, which is what the
        # person saw themselves type.
        return label or target
    target = target.removeprefix("mailto:").removeprefix("tel:")
    return target


def parse_command(text: str) -> Optional[str]:
    """The shell command in a `!!` message, or None if this isn't one.

    Undoes what Slack did to the text on the way here: entity escaping, link
    wrapping, code formatting and smart quotes.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith(PREFIX):
        return None
    cmd = stripped[len(PREFIX) :].strip()
    m = _FENCE_RE.match(cmd) or _TICKS_RE.match(cmd)
    if m:
        cmd = m.group(1).strip()
    cmd = _ANGLE_RE.sub(_unwrap_angle, cmd)
    # After the angle forms are gone: &amp; -> &, &lt; -> <, &gt; -> >.
    cmd = html.unescape(cmd)
    for bad, good in _SMART.items():
        cmd = cmd.replace(bad, good)
    return cmd.strip() or None


class Result:
    """What a finished command produced."""

    def __init__(self, command: str, output: str, exit_code: Optional[int], timed_out: bool,
                 killed: bool, duration_s: float, truncated: bool):
        self.command = command
        self.output = output
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.killed = killed
        self.duration_s = duration_s
        self.truncated = truncated

    def status(self) -> str:
        if self.timed_out:
            return "timed out"
        if self.killed:
            return "stopped"
        return f"exit {self.exit_code}"

    def slack_text(self) -> str:
        """The Slack message for this result. The tail matters more than the
        head (errors land at the end), so long output keeps its end."""
        head = f"`$ {self.command}` — _{self.status()}, {self.duration_s:.1f}s_"
        body = self.output
        clipped = self.truncated
        if len(body) > MAX_SLACK_CHARS:
            body = body[-MAX_SLACK_CHARS:]
            clipped = True
        if not body.strip():
            return f"{head}\n_(no output)_"
        note = "\n_(output clipped — full output attached)_" if clipped else ""
        return f"{head}\n```\n{body}\n```{note}"

    def agent_text(self) -> str:
        """What the agent is told about this command on its next turn."""
        body = self.output
        if len(body) > MAX_AGENT_CHARS:
            body = body[-MAX_AGENT_CHARS:]
        return (
            f"[Shell command the sponsor ran in this thread with `!!` — you did not "
            f"run it, and its output is shown as it was posted to Slack]\n"
            f"$ {self.command}\n({self.status()})\n{body.strip() or '(no output)'}"
        )


class Runner:
    """Runs `!!` commands, one process at a time per thread.

    Keeps each thread's running process so `!stop` can kill it, and the
    finished results the agent hasn't been told about yet.
    """

    def __init__(self, cwd: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 env: Optional[dict[str, str]] = None):
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._env = env or {}
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._pending: dict[str, list[Result]] = {}

    async def run(self, thread_key: str, command: str) -> Result:
        started = time.monotonic()
        env = {**os.environ, **self._env}
        # Own session/process group, so a command that spawns children (a dev
        # server, a test runner) can be killed as a group rather than leaving
        # orphans holding ports.
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self._cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self._running[thread_key] = proc
        timed_out = False
        chunks: list[bytes] = []
        size = 0
        truncated = False
        try:
            async def drain() -> None:
                nonlocal size, truncated
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(8192)
                    if not chunk:
                        return
                    if size < MAX_CAPTURE_CHARS:
                        chunks.append(chunk)
                        size += len(chunk)
                    else:
                        truncated = True  # keep draining so the pipe can't block

            try:
                await asyncio.wait_for(drain(), timeout=self._timeout_s)
                await proc.wait()
            except asyncio.TimeoutError:
                timed_out = True
                self._kill(proc)
                await proc.wait()
        finally:
            if self._running.get(thread_key) is proc:
                del self._running[thread_key]

        output = b"".join(chunks).decode("utf-8", "replace")
        if truncated:
            output = f"(earlier output dropped — over {MAX_CAPTURE_CHARS} chars)\n{output}"
        killed = not timed_out and proc.returncode is not None and proc.returncode < 0
        result = Result(
            command=command,
            output=output,
            exit_code=proc.returncode,
            timed_out=timed_out,
            killed=killed,
            duration_s=time.monotonic() - started,
            truncated=truncated,
        )
        log.info(
            "!! %s -> %s in %.1fs (%d chars)",
            command, result.status(), result.duration_s, len(output),
        )
        queue = self._pending.setdefault(thread_key, [])
        queue.append(result)
        del queue[:-MAX_PENDING_PER_THREAD]
        return result

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        """Kill the command and everything it started (its process group)."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def stop(self, thread_key: str) -> bool:
        """Kill this thread's running command, if any. For `!stop`."""
        proc = self._running.get(thread_key)
        if proc is None:
            return False
        log.info("!! stopping command on %s", thread_key)
        self._kill(proc)
        return True

    def is_running(self, thread_key: str) -> bool:
        return thread_key in self._running

    def take_pending(self, thread_key: str) -> list[Result]:
        """Results not yet shown to the agent for this thread, clearing them."""
        return self._pending.pop(thread_key, [])
