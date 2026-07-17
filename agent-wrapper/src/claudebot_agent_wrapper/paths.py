"""Where the agent-wrapper keeps its per-user runtime state.

Installed as a package, we must NOT write next to our own code (site-packages
is the wrong place and gets wiped on upgrade). State lives in a stable per-user
directory instead: ``$CLAUDEBOT_STATE_DIR`` if set, else ``~/.claudebot``.
"""

from __future__ import annotations

import os


def state_dir() -> str:
    """Return the state directory, creating it if needed."""
    d = os.environ.get("CLAUDEBOT_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claudebot"
    )
    os.makedirs(d, exist_ok=True)
    return d


def state_path(name: str) -> str:
    """Absolute path to a state file by basename."""
    return os.path.join(state_dir(), name)
