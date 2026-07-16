# Claudebot OSS

An open-source, self-hosted version of Claudebot: a Slack-native AI teammate.

Two self-contained apps:

- **`central/`** — a vanilla Node web app + event dispatcher. Receives Slack
  events, persists them to a durable per-agent event log, and pushes them to a
  connected agent over a WebSocket. Multi-tenant across Slack workspaces.
- **`bridge/`** — a Python program that runs in the agent workspace (laptop, VM,
  container — set up by hand). Dials home to Central to register, receives events
  over the WebSocket, and drives Claude Code.

The two talk over one small WebSocket protocol; the contract is documented in
[`docs/DESIGN.md`](docs/DESIGN.md).

## Quick start

**Central** (Node 22+ — uses the built-in `node:sqlite`, no native build):

```bash
cd central
npm install
cp .env.example .env    # then edit
npm start               # http://localhost:3000
```

**Bridge** (Python 3.11+ with [`uv`](https://docs.astral.sh/uv/), plus `claude`
and `gh` installed):

```bash
cd bridge
cp .env.example .env    # set CENTRAL_URL + REGISTRATION_TOKEN
uv run python bridge.py
```

## License

AGPL-3.0-or-later. See [`LICENSE`](LICENSE).
