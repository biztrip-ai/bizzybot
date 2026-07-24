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
uv tool install "git+https://github.com/biztrip-ai/bizzybot.git#subdirectory=agent-wrapper"
```

(or `pipx install "git+https://github.com/biztrip-ai/bizzybot.git#subdirectory=agent-wrapper"`).
This puts a `bizzybot` command on your PATH. To update later:
`uv tool upgrade bizzybot-agent-wrapper`.

## Run

```bash
bizzybot
```

On first run it **prompts for your registration token** (get it by signing in at
the Central-Dispatch dashboard) and caches it in `~/.bizzybot/agent-wrapper-config.json`,
so later runs need no arguments. `CENTRAL_URL` defaults to the hosted Bizzybot —
override it (env, `.env`, or the saved config) only to point at your own
Central-Dispatch. You can also skip the prompt by setting `REGISTRATION_TOKEN` in
the environment or `.env`:

```bash
CENTRAL_URL=https://your-central-dispatch REGISTRATION_TOKEN=<token> bizzybot
```

### From source

```bash
git clone https://github.com/biztrip-ai/bizzybot.git
cd bizzybot/agent-wrapper
uv run bizzybot
```

On start it:

1. **Registers** with Central-Dispatch (`POST /api/register`) using your registration
   token, and pulls the Slack bot token + WebSocket details. If a cached token is
   rejected, it re-prompts.
2. Runs a **preflight** — checks Claude Code (fatal if missing), `gh` auth, and
   git identity (warnings).
3. Opens the **WebSocket** and replays anything it missed (via the last acked
   `seq` in `~/.bizzybot/agent-wrapper-state.json`), then handles live events.

## Agent settings file

Per-agent settings live in `~/.bizzybot/settings.env` (override the path with
`BIZZYBOT_SETTINGS_FILE`) — a dotenv-style file you edit by hand. Every key in
it is passed through to each `claude` subprocess as an environment variable, so
it's also the place for provider keys or other per-agent env. Real environment
variables win over the file, and changes take effect on the next restart.

### Using OpenRouter as the model provider

Set both keys and the agent runs its Claude Code sessions against OpenRouter
instead of Anthropic ([OpenRouter's Claude Code
guide](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration)):

```bash
# ~/.bizzybot/settings.env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
```

The agent-wrapper then points the subprocess at `https://openrouter.ai/api`,
passes the key as `ANTHROPIC_AUTH_TOKEN`, blanks `ANTHROPIC_API_KEY` (an
inherited Anthropic key would otherwise take precedence), and pins
`OPENROUTER_MODEL` for the main session, subagents, and Claude Code's internal
opus/sonnet/haiku tiers. `CLAUDE_MODEL` is ignored while OpenRouter is
configured. Setting only one of the two keys is ignored with a warning.

Any model on OpenRouter works, but tool-calling quality varies — start with an
`anthropic/*` model. Note that Claude Code's own `/logout` state matters: if the
`claude` CLI is signed in with an Anthropic account, run `claude /logout` once so
it doesn't prefer those cached credentials over the OpenRouter token.

## Behaviour

- Responds to **@-mentions** and **direct messages**.
- Streams replies into a single Slack message, edited in place.
- Meta commands: `!stop` (interrupt the running turn), `!clear` (reset the
  thread's session), `!help`.
- **Serialized turns:** a second message in a thread whose turn is still running
  waits behind it, showing a *"⏳ queued…"* placeholder until it starts.
- **Background sub-agent flush:** if the agent launches background sub-agents
  and ends its turn, their finishing (the `SubagentStop` hook) wakes the idle
  session and an automatic turn posts the results to the thread — no user
  message needed. A flush with nothing new to say posts nothing. Background
  *shell* tasks have no equivalent hook and still surface only on the next
  message.
- **Shared working directory:** every thread runs in the same `CLAUDE_CWD`, and
  turns in different threads run concurrently, so two threads editing the same
  checkout would collide (and one switching branches would strand the other's
  work). The appended system prompt tells the agent to `git worktree add` its own
  tree before editing code, to remove it once the changes are committed, and to
  kill any dev server it started. This is advisory — the model follows the
  instruction; nothing enforces it. For a hard guarantee, run one agent-wrapper
  per checkout (separate `CLAUDE_CWD` *and* `BIZZYBOT_STATE_DIR`).
- **Idle-session eviction:** each thread pins an ~80–130 MB `claude` subprocess.
  A background reaper closes sessions idle longer than `SESSION_IDLE_TIMEOUT_S`
  (default `14400` = 4h; `0` disables), scanning every `SESSION_REAP_INTERVAL_S`
  (default `300`). The thread's resume id is kept, so the next message in a
  reaped thread transparently resumes the same conversation.
- Events are acked by sequence; if the agent-wrapper is offline, Central-Dispatch holds events
  and replays them on reconnect.

## State files

Kept in `~/.bizzybot/` (override with `BIZZYBOT_STATE_DIR`):

- `agent-wrapper-config.json` — cached registration token (+ Central-Dispatch URL),
  written on first-run prompt. Holds a secret; kept `0600`.
- `agent-wrapper-state.json` — last acked event sequence.
- `sessions.json` — per-thread Claude session ids (for resume across restarts).
- `settings.env` — your agent settings (see above). Hand-edited, not written by
  the agent-wrapper.
