"""Where the agent-wrapper keeps its per-user runtime state.

Installed as a package, we must NOT write next to our own code (site-packages
is the wrong place and gets wiped on upgrade). State lives in a stable per-user
directory instead: ``$BIZZYBOT_STATE_DIR`` if set, else ``~/.bizzybot``.
"""

from __future__ import annotations

import os


def state_dir() -> str:
    """Return the state directory, creating it if needed."""
    d = os.environ.get("BIZZYBOT_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".bizzybot"
    )
    os.makedirs(d, exist_ok=True)
    return d


def state_path(name: str) -> str:
    """Absolute path to a state file by basename."""
    return os.path.join(state_dir(), name)


def log_path(name: str) -> str:
    """Absolute path to a log file by basename, in a ``logs/`` subdirectory.

    Kept out of the state dir proper so the one-file-per-run logs don't bury
    ``sessions.json`` and friends. Created ``0700``: at ``LOG_LEVEL=DEBUG`` these
    files hold Slack message text and tool arguments, so they get the same
    treatment as the ``0600`` config file that holds the registration token.

    Absolute even when BIZZYBOT_STATE_DIR is relative, so the path we log at
    startup is one you can paste into an ``ssh``/``tail`` command.
    """
    d = os.path.join(state_dir(), "logs")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.abspath(os.path.join(d, name))
