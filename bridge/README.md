# Bridge

Runs in the agent workspace (laptop / VM / container, set up by hand). Dials home
to Central, receives Slack events over a WebSocket, and drives Claude Code — one
persistent session per Slack thread — posting replies back to Slack.

Adapted from the original codespace bridge, minus Ably, cloud provisioning, idle
keep-alive, and bot-token plumbing. It uses your own local `git`/`gh` auth.

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- [Claude Code](https://claude.com/product/claude-code) installed and signed in
- `gh` installed and authenticated (for GitHub work)

## Run

```bash
cp .env.example .env      # set CENTRAL_URL and REGISTRATION_TOKEN
uv run python bridge.py
```

On start it:

1. **Registers** with Central (`POST /api/register`) using your registration
   token, and pulls the Slack bot token + WebSocket details.
2. Runs a **preflight** — checks Claude Code (fatal if missing), `gh` auth, and
   git identity (warnings).
3. Opens the **WebSocket** and replays anything it missed (via the last acked
   `seq` in `.bridge-state.json`), then handles live events.

## Behaviour

- Responds to **@-mentions** and **direct messages**.
- Streams replies into a single Slack message, edited in place.
- Meta commands: `!stop` (interrupt the running turn), `!clear` (reset the
  thread's session), `!help`.
- Events are acked by sequence; if the bridge is offline, Central holds events
  and replays them on reconnect.

## State files (gitignored)

- `.bridge-state.json` — last acked event sequence.
- `.sessions.json` — per-thread Claude session ids (for resume across restarts).
