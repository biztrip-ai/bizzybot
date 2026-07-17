# Claudebot

Integrate a Claude Code agent into your Slack workspace.

## Getting started

The easiest way to run is to use our pre-created Slack app and `central-dispatch` event service which is
running on Railway. 

Just visit:

[https://claudebot-production-34ba.up.railway.app](https://claudebot-production-34ba.up.railway.app)

login with Slack, add the Slack app to your workspace, then run the agent-wrapper on your machine.


## Key steps

1. Sign-in with your Slack account
2. Add the Slack app to your workspace
3. Configure `Claude Code` on your local machine (see below)
4. Run the `agent-wrapper` on your local machine
5. Enter your 'registration key' into the agent-wrapper: this connects it to the Slack listener 

That's it! Invite the Slack app (one of `@cosmo`, `@bizzy` or `@omni`) into a channel and send it
some requests.

## Setting up Claude Code

1. Get Claude Code installed and authenticated, you will need to add an API key or login with a subscription.

2. Setup browser automation via `Claude-in-Chrome` (best) or `Chrome devtools` MCP.

3. Make sure the Github CLI `gh` is installed and authenticated into Github. It should be on the path
so it's usable by Claude.

## Pushing code from your agent

We created a separate Github user account to use for our bot. This gives you bot a full Github idenity but
costs you a seat.

You can also use a Github App instead and configure the bot as a Github bot user.


# Running from source

Two self-contained apps:

- **`central-dispatch/`** — a vanilla Node web app + event dispatcher. Receives Slack
  events, persists them to a durable per-agent event log, and pushes them to a
  connected agent over a WebSocket. Multi-tenant across Slack workspaces.
- **`agent-wrapper/`** — a Python program that runs in the agent workspace (laptop, VM,
  container — set up by hand). Dials home to Central-Dispatch to register, receives events
  over the WebSocket, and drives Claude Code.

The two talk over one small WebSocket protocol; the contract is documented in
[`docs/DESIGN.md`](docs/DESIGN.md).

## Quick start

**Central-Dispatch** (Node 22+ — uses the built-in `node:sqlite`, no native build):

```bash
cd central-dispatch
npm install
cp .env.example .env    # then edit
npm start               # http://localhost:3000
```

**Agent-Wrapper** (Python 3.11+ with [`uv`](https://docs.astral.sh/uv/), plus `claude`
and `gh` installed). Install the `claudebot` command, then run it:

```bash
uv tool install "git+https://github.com/biztrip-ai/claudebot.git#subdirectory=agent-wrapper"
CENTRAL_URL=http://localhost:3000 REGISTRATION_TOKEN=<token> claudebot
```

Or run from source: `cd agent-wrapper && uv run claudebot`.

## License

AGPL-3.0-or-later. See [`LICENSE`](LICENSE).
