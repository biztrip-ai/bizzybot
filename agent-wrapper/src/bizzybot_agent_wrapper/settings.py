"""User settings file for an agent.

A dotenv-style `KEY=value` file (default ``~/.bizzybot/settings.env``) that a
workspace owner edits by hand to customize their agent without touching the
agent-wrapper's own .env or the process environment. The real environment wins
on conflicts, so a value set in the shell/.env still overrides the file.

Today it carries the model-provider settings; anything else in it is passed
through to the spawned `claude` subprocess as-is, which makes it the place to
put provider keys or other per-agent env.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .paths import state_path

log = logging.getLogger("agent-wrapper.settings")

SETTINGS_FILE = "settings.env"

# Keys we never echo in full when logging what was loaded.
_SECRET_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# The OpenRouter setup Claude Code expects, per
# https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration
OPENROUTER_BASE_URL = "https://openrouter.ai/api"


def settings_path() -> str:
    """Path to the settings file (``$BIZZYBOT_SETTINGS_FILE`` overrides)."""
    return os.environ.get("BIZZYBOT_SETTINGS_FILE") or state_path(SETTINGS_FILE)


def load_settings(path: Optional[str] = None) -> dict[str, str]:
    """Parse the settings file into a dict. Missing file => empty dict.

    Accepts `KEY=value`, `export KEY=value`, `#` comments, blank lines, and
    single/double-quoted values. Malformed lines are warned about and skipped
    rather than failing the agent's startup.
    """
    p = path or settings_path()
    try:
        with open(p) as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return {}

    out: dict[str, str] = {}
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            log.warning("%s:%d: ignoring malformed line", p, n)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value

    if out:
        log.info("loaded %d setting(s) from %s: %s", len(out), p, ", ".join(sorted(out)))
    return out


def resolve(settings: dict[str, str], key: str) -> Optional[str]:
    """Value for `key`: the real environment first, then the settings file."""
    return (os.environ.get(key) or settings.get(key) or "").strip() or None


def _redact(key: str, value: str) -> str:
    if any(h in key.upper() for h in _SECRET_HINT):
        return f"…{value[-4:]}" if len(value) > 4 else "…"
    return value


def claude_env(settings: dict[str, str]) -> tuple[dict[str, str], Optional[str]]:
    """Build the environment overrides for the spawned `claude`, plus the model
    to pin (None => leave Claude Code's default).

    Every setting is passed through to the subprocess. If both
    OPENROUTER_API_KEY and OPENROUTER_MODEL are configured, we additionally
    point Claude Code at OpenRouter's Anthropic-compatible endpoint: base URL +
    the key as the auth token, with ANTHROPIC_API_KEY explicitly emptied so a
    stray Anthropic key in the environment can't take precedence over it.
    """
    env = dict(settings)

    api_key = resolve(settings, "OPENROUTER_API_KEY")
    model = resolve(settings, "OPENROUTER_MODEL")
    if not (api_key and model):
        if api_key or model:
            log.warning(
                "OpenRouter needs both OPENROUTER_API_KEY and OPENROUTER_MODEL "
                "(only %s is set) — using Claude Code's default provider",
                "OPENROUTER_API_KEY" if api_key else "OPENROUTER_MODEL",
            )
        return env, None

    env.update({
        "ANTHROPIC_BASE_URL": OPENROUTER_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        # Must be explicitly empty, or Claude Code prefers it over the auth token.
        "ANTHROPIC_API_KEY": "",
        # Claude Code resolves the opus/sonnet/haiku tier names for its own
        # internal calls (subagents, summarization); route them all at the one
        # model the user chose so nothing falls back to an Anthropic model id
        # OpenRouter won't recognize.
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
    })
    log.info(
        "model provider: OpenRouter (%s) model=%s key=%s",
        OPENROUTER_BASE_URL, model, _redact("OPENROUTER_API_KEY", api_key),
    )
    return env, model
