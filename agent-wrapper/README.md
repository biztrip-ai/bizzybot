# Agent-Wrapper

Runs in the agent workspace (laptop / VM / container, set up by hand). Dials home
to Central-Dispatch, receives Slack events over a WebSocket, and drives Claude Code — one
persistent session per Slack thread — posting replies back to Slack.

Adapted from the original codespace agent-wrapper, minus Ably, cloud provisioning, idle
keep-alive, and bot-token plumbing. It uses your own local `git`/`gh` auth.

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) (or `pipx`)
- [Claude Code](https://claude.com/product/claude-code) installed and signed in
- `gh` installed and authenticated (for GitHub work)

## Install

```bash
uv tool install "git+https://github.com/biztrip-ai/claudebot.git#subdirectory=agent-wrapper"
```

(or `pipx install "git+https://github.com/biztrip-ai/claudebot.git#subdirectory=agent-wrapper"`).
This puts a `claudebot` command on your PATH. To update later:
`uv tool upgrade claudebot-agent-wrapper`.

## Run

```bash
claudebot
```

On first run it **prompts for your registration token** (get it by signing in at
the Central-Dispatch dashboard) and caches it in `~/.claudebot/agent-wrapper-config.json`,
so later runs need no arguments. `CENTRAL_URL` defaults to the hosted Claudebot —
override it (env, `.env`, or the saved config) only to point at your own
Central-Dispatch. You can also skip the prompt by setting `REGISTRATION_TOKEN` in
the environment or `.env`:

```bash
CENTRAL_URL=https://your-central-dispatch REGISTRATION_TOKEN=<token> claudebot
```

### From source

```bash
git clone https://github.com/biztrip-ai/claudebot.git
cd claudebot/agent-wrapper
uv run claudebot
```

On start it:

1. **Registers** with Central-Dispatch (`POST /api/register`) using your registration
   token, and pulls the Slack bot token + WebSocket details. If a cached token is
   rejected, it re-prompts.
2. Runs a **preflight** — checks Claude Code (fatal if missing), `gh` auth, and
   git identity (warnings).
3. Opens the **WebSocket** and replays anything it missed (via the last acked
   `seq` in `~/.claudebot/agent-wrapper-state.json`), then handles live events.

## Behaviour

- Responds to **@-mentions** and **direct messages**.
- Streams replies into a single Slack message, edited in place.
- Meta commands: `!stop` (interrupt the running turn), `!clear` (reset the
  thread's session), `!help`.
- **Serialized turns:** a second message in a thread whose turn is still running
  waits behind it, showing a *"⏳ queued…"* placeholder until it starts.
- **Idle-session eviction:** each thread pins an ~80–130 MB `claude` subprocess.
  A background reaper closes sessions idle longer than `SESSION_IDLE_TIMEOUT_S`
  (default `14400` = 4h; `0` disables), scanning every `SESSION_REAP_INTERVAL_S`
  (default `300`). The thread's resume id is kept, so the next message in a
  reaped thread transparently resumes the same conversation.
- Events are acked by sequence; if the agent-wrapper is offline, Central-Dispatch holds events
  and replays them on reconnect.

## State files

Kept in `~/.claudebot/` (override with `CLAUDEBOT_STATE_DIR`):

- `agent-wrapper-config.json` — cached registration token (+ Central-Dispatch URL),
  written on first-run prompt. Holds a secret; kept `0600`.
- `agent-wrapper-state.json` — last acked event sequence.
- `sessions.json` — per-thread Claude session ids (for resume across restarts).
