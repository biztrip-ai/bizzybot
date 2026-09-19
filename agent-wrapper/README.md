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

- Responds to **@-mentions**, **direct messages**, and every message in
  channels the bot created.
- Streams replies into a single Slack message, edited in place.
- Meta commands: `!stop` (interrupt the running turn), `!clear` (reset the
  thread's session), `!help`.
- **`!!` shell commands:** a message starting with `!!` runs the rest as a
  shell command in `CLAUDE_CWD` and posts the output back — no Claude turn
  involved. **Only the agent's sponsor may do this**, and only when a sponsor
  is set. Slack's escaping, link wrapping, code formatting and smart quotes are
  undone first. One command at a time per thread; `!stop` kills it and
  everything it started. Output over ~2400 chars is shown as a tail with the
  whole thing attached as a file, and the command and its output are handed to
  the agent with your next message in that thread (like Claude Code's own `!`
  prefix), so `!! npm test` then "fix those failures" works. Tunable with
  `SHELL_TIMEOUT_S` (default 120); `SHELL_COMMANDS=0` disables it.
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
- **Channels the bot created:** in a channel the bot created itself (Slack's
  `creator` is the bot), it responds to every top-level post, not just
  @-mentions, and answers at top level rather than in a thread. Those posts
  share one conversation per channel. An @-mention still gets a threaded
  reply, and only threads started that way get follow-up replies without a
  re-mention. System notices (joins, topic changes…) are ignored. The
  check uses `conversations.info`, so it needs the `channels:read` /
  `groups:read` scopes; without them those channels behave like any other.
- **Sponsor:** the human responsible for this agent — the Slack user who
  installed it (Central-Dispatch keeps it and returns it at registration; the
  dashboard can hand it over). The system prompt names them, so the agent knows
  whose call settles an ambiguous or risky request. `SPONSOR_SLACK_USER_ID`
  overrides it locally for development.
- **Sender identity:** each Slack message reaches the agent with a first line
  naming who wrote it, e.g. `[Slack message from Jane Doe (@jane), user ID
  U0123 — mention as <@U0123>]`, so it can mention or invite that person
  without asking. Names come from `users.info` and are cached for an hour.
- **Slack workspace tools:** the agent gets an in-process MCP server,
  `bizzybot`, backed by the workspace's bot token: `list_channels`,
  `list_users`, `create_channel` (with optional topic, purpose and
  invites) and `archive_channel` (Slack doesn't let bots delete channels, and
  the bot must be a member of the channel it archives). Set `CLAUDE_SLACK_MCP=0` to turn it off. It needs the
  `channels:read`, `groups:read`, `channels:manage` and `groups:write` bot
  scopes; a workspace installed before those were added gets a `missing_scope`
  tool error until an admin clicks **Reinstall** on the app's dashboard card
  and the agent-wrapper is restarted.
- Events are acked by sequence; if the agent-wrapper is offline, Central-Dispatch holds events
  and replays them on reconnect.

## Multi-agent hand-offs

Several agents can pass work to each other in Slack (e.g. a PM agent
dispatching a builder agent):

- **Waking on another agent:** messages from bots are normally ignored. Set
  `AGENT_MENTIONS_FROM` to the other agents' Slack user, app or bot ids. A
  message from one of them wakes this agent only if it @-mentions it with a
  real `<@U…>` token. The model is told the message came from an agent.
- **Silence ends the exchange:** when another agent starts a turn and the reply
  is empty, the "thinking…" placeholder is deleted and nothing is posted.
  A human's message still gets "_nothing new to report_".
- **Loop guard:** `AGENT_CHAIN_LIMIT` (default 25) caps how many agent-triggered
  turns can run in a row. Any human message the agent sees resets the count,
  and so does a quiet gap of `AGENT_CHAIN_WINDOW_S` (default 600 s).
- **Role file:** `AGENT_PROMPT_FILE` appends a file, such as a role protocol, to
  every session's system prompt. It's re-read whenever a conversation starts.
- **Tools:** the Slack tools include `post_message` (to another channel or
  thread, with optional files), `read_messages` (a channel's or thread's recent
  messages) and `add_reaction`. `add_reaction` needs the `reactions:write`
  scope, so reinstall the app from the dashboard to grant it.

## Receiving secrets: `bizzybot-dropbox`

To get a token, key or env var value onto an agent box without pasting it into
chat or a terminal, request it through a one-time dropbox page on
Central-Dispatch:

```sh
bizzybot-dropbox request --restart \
  --note "Tokens for the new BzPM box" \
  'env:REGISTRATION_TOKEN::48-char token from the BzPM card on the dashboard' \
  'settings:OPENROUTER_API_KEY' \
  'file:deploy_key:/workspaces/.ssh/deploy_key::private key, PEM'
```

The command prints a URL and a check code, then waits. Whoever opens the URL
sees the note, the check code (it should match the one printed here) and one
field per item. Their browser encrypts the values to a key pair generated on
this box, so Central-Dispatch only ever relays ciphertext. The box then
decrypts the values, installs them and prints only names and lengths:

| Spec | Installed to |
|---|---|
| `env:NAME` | upserted in `/workspaces/env/agent.env` if that exists, otherwise `$BIZZYBOT_STATE_DIR/.env` (or `--env-file`) |
| `settings:NAME` | upserted in `$BIZZYBOT_STATE_DIR/settings.env` |
| `file:NAME:/abs/path` | written to that file, mode 0600 (multi-line) |

The link can be used once and expires (`--ttl`, default 30 min). The page
needs no sign-in: anyone with the link can *submit*, but nobody can read.
The box installs nothing unless the submission holds exactly the names it
requested, and env values must be a single line. It works before the agent
has registered, so it can deliver `REGISTRATION_TOKEN` itself.

## State files

Kept in `~/.bizzybot/` (override with `BIZZYBOT_STATE_DIR`):

- `agent-wrapper-config.json` — cached registration token (+ Central-Dispatch URL),
  written on first-run prompt. Holds a secret; kept `0600`.
- `agent-wrapper-state.json` — last acked event sequence.
- `sessions.json` — per-thread Claude session ids (for resume across restarts).
- `settings.env` — your agent settings (see above). Hand-edited, not written by
  the agent-wrapper.
- `logs/` — one log file per run (see below). May hold sensitive text; kept `0700`.

## Logs

Logs go to the console *and* to a file, so you can read them back on a machine
you aren't sitting in front of:

```
ssh agent-laptop 'tail -F ~/.bizzybot/logs/agent-wrapper-20260729-153012-4711.log'
```

Use `tail -F`, not `-f`, and name one file rather than globbing. `-f` follows the
file descriptor, so the moment the size cap rotates your file to `.log.1` it goes
silent — looking exactly like an idle agent while the run keeps logging. And a
`*.log` glob expands over every past run, since nothing is pruned.

Each run gets its own file, named for the moment it started plus the pid:
`~/.bizzybot/logs/agent-wrapper-20260729-153012-4711.log`. The names sort
chronologically, and no two runs can collide on one.

A file is capped at 1 MiB (`BIZZYBOT_LOG_MAX_BYTES`); past that it rotates once,
so a long-running agent keeps its most recent megabyte in the `.log` and the one
before it in `.log.1`, and drops anything older. If the logs dir isn't writable,
the run carries on console-only rather than refusing to start.

`LOG_LEVEL=DEBUG` captures the model's full reply text and thinking blocks. It
sets the *root* level, so you also get the Slack and HTTP client's own debug
output — request params, response bodies, channel and user ids (the `slack-sdk`
redacts `Authorization`, so your bot token stays out). Useful, but it fills that
megabyte fast, and it puts a lot of conversation content on disk.

Set both of these in your shell environment or `.env` — **not** in
`settings.env`, which is read too late in startup and only forwarded to the
`claude` subprocess, so logging never sees it.

Old run logs are never deleted; prune `~/.bizzybot/logs/` yourself if it grows.
Note the `claude` subprocess's own stderr goes to the console only — it is not
captured in the file.
