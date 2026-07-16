# Claudebot

Integrate a Claude Code agent into your Slack workspace.

## Getting started

The easiest way to run is to use our pre-created Slack apps and `eventmgr` app which is
running on Railway. 

Just visit:

     https://claudebot-production-34ba.up.railway.app

and app the Slack app, then run the agent-wrapper on your machine.

## Running from source

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
and `gh` installed):

```bash
cd agent-wrapper
cp .env.example .env    # set CENTRAL_URL + REGISTRATION_TOKEN
uv run python agent_wrapper.py
```

## License

AGPL-3.0-or-later. See [`LICENSE`](LICENSE).
