# Claudebot OSS — High-Level Design

**Status:** Draft
**Date:** 2026-07-16

## What this is

An open-source, monorepo version of Claudebot. It keeps the core idea — an AI
teammate you talk to in Slack — and stays a **hosted, multi-tenant service** (one
deployment, e.g. on Railway, serves many independent Slack workspaces). What it
strips out is the **cloud workspace machinery**: Central-Dispatch provisions no compute;
each tenant runs their own agent (see [Multi-tenancy](#multi-tenancy)). In this
version:

- **Central-Dispatch** is a basic web app + event dispatcher.
- The event transport to the agent is a **direct WebSocket** from Central-Dispatch,
  backed by a **persisted per-agent event log** so events survive while the
  agent is disconnected. This replaces the Ably queue. **No message broker** in
  the default design.
- Central-Dispatch does **not** provision or manage any cloud workspace. The agent
  workspace is **set up by hand** (laptop, VM, container — the operator's
  choice).
- The agent **calls back to Central-Dispatch to register**, and during registration
  **retrieves the Slack token** it needs.
- A **new agent-wrapper** runs in the agent workspace, connects to Central-Dispatch over a
  WebSocket, receives events, and drives the coding agent.

It can still rely on the existing pre-built Slack app(s) and webhooks — only the
webhook **hostname** changes to point at this Central-Dispatch.

## Non-goals (explicitly out of scope for this version)

- No Codespace / cloud workspace provisioning or lifecycle management. (Central-Dispatch
  is still multi-tenant across Slack workspaces — it just doesn't provision the
  *compute*; each tenant runs their own agent-wrapper.)
- No billing or "hire a team" dashboard.
- No Ably, and no message broker (NATS/Kafka/etc.) in the default path.
- No per-teammate cloud identity juggling.
- No idle keep-alive / codespace restart-resume machinery (the workspace is
  long-lived and operator-managed).

## Multi-tenancy

Central-Dispatch is a hosted, multi-tenant service. The tenant boundary is the **Slack
workspace**; one Central-Dispatch serves many.

- **One shared, distributable Slack app** is installed into each workspace via
  the "Add to Slack" OAuth flow. Each install creates (or reuses) one **agent**
  row keyed by the workspace's `team_id`, holding that workspace's bot token and
  a secret registration token.
- **The `agents` table is the tenant registry** — there is no separate
  accounts/users table. Completing OAuth for a workspace is the authorization
  boundary (only someone who can install the app in that workspace reaches it),
  and re-running "Add to Slack" re-shows that tenant's registration token, so a
  tenant can always recover it.
- **Isolation is per agent, end to end.** Each agent has its own event log, its
  own registration token, and its own WebSocket. Inbound Slack events are routed
  ONLY to the agent whose `team_id` matches the envelope — never broadcast. A
  tenant's agent-wrapper authenticates with its registration token and can reach only
  its own agent's stream.
- **Compute is not multi-tenant here** — Central-Dispatch provisions nothing. Each tenant
  runs their own agent-wrapper (laptop / VM / container) that dials home, so their code
  never leaves their machine and Central-Dispatch stays light.

One agent per workspace for now; multiple teammates per workspace is a future
extension. Supporting several distinct Slack apps (personas) would add an
app registry keyed by `api_app_id` (as the original Central-Dispatch had) to pick the
signing secret per app — deferred; the default is a single shared app.

## Architecture at a glance

```
          Slack Events API (webhook, new hostname)
                         │
                         ▼
   ┌─────────────────────────────────────────────┐
   │                CENTRAL-DISPATCH                │
   │                                                │
   │  Slack webhook receiver                        │
   │        │                                       │
   │        ▼                                       │
   │  Event log (append-only, per-agent seq)        │
   │        │                                 ▲     │
   │        ▼                                 │acks │
   │  WebSocket hub ── live push when connected     │
   │  Registration API                              │
   │  (agent calls back, gets Slack token)          │
   └───────────────┬──────────────┬────────────────┘
                   │ register      │ WebSocket
                   ▼               ▼
   ┌─────────────────────────────────────────────┐
   │        AGENT WORKSPACE (set up by hand)       │
   │   AGENT-WRAPPER ── WS client; sends seq        │
   │      │        on connect, acks as it consumes │
   │      ├──► drives the coding agent (Claude Code)│
   │      └──► posts replies to Slack (token)       │
   └─────────────────────────────────────────────┘
```

No message broker. Central-Dispatch pushes events straight to the connected agent-wrapper over
the WebSocket for minimal latency, and persists every event to its own store so
nothing is lost while the agent is offline. The agent connection is
**outbound-only** (firewall/NAT friendly), consistent with the dial-home model.

### Why no broker by default

The default and most important case is an agent **always running on a laptop**,
already connected over the WebSocket. For that case a broker only adds a hop and
latency, so Central-Dispatch pushes events directly over the WS.

The one thing a broker would have given us for free — **durability while the
client is disconnected** (for the future "cloud workspace that might be off"
case) — we get instead from a small **append-only event log** in Central-Dispatch's own
database plus a **per-agent delivery cursor**:

- Every inbound event is appended with a monotonic per-agent sequence number.
- If the agent's WS is connected, push it immediately (minimal latency).
- If not, it simply waits in the log.
- On (re)connect the agent-wrapper sends its last-seen sequence; Central-Dispatch replays
  everything after it in order, then switches to live tailing.
- The agent-wrapper acks as it consumes so Central-Dispatch can advance the cursor and prune.

This is a durable log with a cursor — the same shape a broker gives you — but
implemented in the database Central-Dispatch already needs, with zero extra infra.

**Where NATS could come back:** if we later want multi-instance Central-Dispatch, fan-out
to many agents, or want to stop maintaining the log ourselves, NATS JetStream
fits well — it is durable and supports WebSocket clients (nats.ws) directly, so
it could collapse the WS hub + event log into one component. It stays an
optional future transport, not a day-one dependency.

## Setup flow (first run)

The user-facing onboarding. It is Slack-first at the front and hand-run at the
back — no cloud workspace is provisioned.

1. **Install the Slack app / sign in.** The user adds the pre-built Slack app
   (new hostname) and signs in with Slack. This establishes the workspace
   (`team_id` as the tenant anchor) and creates the agent record in Central-Dispatch.
2. **Central-Dispatch shows a registration token.** After sign-in, Central-Dispatch generates and
   displays a one-time **registration token** for the user to copy. The agent-wrapper
   uses it to authenticate to Central-Dispatch and pull the actual Slack bot token.
3. **Instructions to install the agent-wrapper.** Central-Dispatch shows the command to run,
   e.g. `npx @claudebot/agent-wrapper`.
4. **Start the agent-wrapper, paste the token.** The user runs the agent-wrapper in their
   chosen workspace (laptop / VM / container) and enters the registration token
   when prompted.
5. **Agent-Wrapper connects and self-checks.** The agent-wrapper:
   - calls back to Central-Dispatch with the token (dial-home registration),
   - downloads the Slack token(s) and its config,
   - runs a local **preflight** — verifies Claude Code is installed, verifies
     `gh` is installed and authenticated, checks git config, and reports any
     gaps with fix hints,
   - opens the WebSocket to Central-Dispatch and comes online (optionally announcing
     itself in Slack).

If preflight fails, the agent-wrapper reports what's missing in the terminal (and can
surface it in Slack) and stays in a not-ready state until the operator fixes it.
Steps 4–5 are the user-facing view of **Core flow 1 (Registration)** below.

## Core flows

### 1. Registration (dial-home)

1. The operator sets up the agent workspace by hand and starts the agent-wrapper,
   pasting the **registration token** shown by Central-Dispatch at sign-in.
2. The agent-wrapper POSTs to Central-Dispatch's registration endpoint with the token.
3. Central-Dispatch validates it and returns: the agent's identity, the **Slack bot
   token**, and WebSocket connection details.
4. The agent-wrapper runs its local **preflight** (Claude Code, `gh` auth, git config)
   and reports any gaps.
5. The agent-wrapper opens a WebSocket to Central-Dispatch, sending its **last-seen sequence**
   (0 on first run), and is now live.

### 2. Inbound event (Slack → agent)

1. Slack posts an event to Central-Dispatch's webhook (new hostname).
2. Central-Dispatch **appends** it to the agent's event log with the next sequence
   number.
3. If the agent's WebSocket is connected, Central-Dispatch pushes it immediately;
   otherwise it waits in the log.
4. The agent-wrapper receives it, hands it to the coding agent, and acks the sequence.

### 3. Reconnect / replay (the cloud-off case)

1. The agent-wrapper reconnects and sends its last-seen (last-acked) sequence.
2. Central-Dispatch replays every event after that sequence, in order.
3. Once caught up, Central-Dispatch resumes live push.

### 4. Outbound reply (agent → Slack)

The agent-wrapper posts the reply to Slack **directly** using the Slack token it
retrieved at registration. (Alternative — send it back up the WebSocket and let
Central-Dispatch post it — is noted in Open Questions.)

## Repo layout

Two self-contained apps at the top level — no workspace/monorepo tooling. The
WS message contract is tiny, so each side keeps its own copy (Central-Dispatch in
`central-dispatch/src/protocol.js`, the agent-wrapper inline) rather than sharing a package.

```
oss-version/
  central-dispatch/            # Node web app + event dispatcher (Slack webhook, OAuth,
                      #   event log, WS hub, registration API). deps: express, ws
                      #   + built-in node:sqlite. Standalone npm package.
  agent-wrapper/             # Python agent-side connector (register, WS client,
                      #   preflight, drives Claude Code).
  docs/
    DESIGN.md         # this file
  README.md
  LICENSE             # AGPL
```

## Key decisions

- **No message broker by default** — direct WebSocket push for minimal latency
  in the always-connected laptop case, plus a persisted per-agent event log for
  durability while disconnected. NATS/JetStream stays an optional future
  transport if we need multi-instance fan-out or want to offload the log.
- **At-least-once delivery with a cursor** — events carry per-agent sequence
  numbers and are acked; replay-on-reconnect covers disconnects. The coding
  agent should tolerate an occasional duplicate.
- **Dial-home registration** — Central-Dispatch never reaches into the workspace; the
  agent connects out and pulls the config (including the Slack token) it needs.
- **Reuse the existing Slack app** — only the webhook hostname changes; no new
  Slack app is strictly required to stand this up.

## Running on Railway

Central-Dispatch is meant to be deployed once and shared by many tenants (Railway is the
reference host).

- Deploy Central-Dispatch as a **single service**. Set `PUBLIC_URL` to the Railway
  domain and the Slack env vars (`SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`,
  `SLACK_SIGNING_SECRET`). The one shared Slack app's webhook + OAuth redirect
  point at that domain.
- **Persist the event log.** Best option: set `DATABASE_URL` to a managed
  Postgres (e.g. Neon) — Central-Dispatch then stores everything in a dedicated schema
  (`DB_SCHEMA`, default `claudebot`) that survives redeploys and can share a
  database with other apps. Alternatively, keep SQLite but mount a Railway volume
  and point `DB_PATH` at it (`/data/central-dispatch.db`); without one, a redeploy wipes
  the file and every tenant's agent loses its cursor + buffered events.
- **Single instance to start.** The WebSocket hub and the CSRF state map are
  in-process, so agent connections must all land on one instance. Horizontal
  scale later needs either sticky WS routing + shared Postgres, or a shared
  pub/sub — which is exactly where NATS/JetStream would return (see the
  transport note). Not needed for launch.

## Open questions

- **Outbound Slack path:** agent posts directly with the retrieved token
  (simplest, asymmetric) vs. everything routes bidirectionally over the
  WebSocket through Central-Dispatch (symmetric, Central-Dispatch holds the token). Starting
  assumption: direct post from the agent.
- **Event store backend:** both supported — SQLite (default) or Postgres via
  `DATABASE_URL` (schema-isolated). Postgres removes the storage side of the
  single-instance constraint; the remaining blocker to multi-instance is the
  in-process WS hub, which still needs a shared pub/sub (where NATS/JetStream
  would return).
- **Multiple teammates per workspace:** the model is one agent per workspace
  today; supporting several (and the several-Slack-apps/persona case via an
  `api_app_id` registry) is a future extension.
- **Coding agent engine:** hard-wire Claude Code, or leave the agent-wrapper's agent
  driver pluggable?

## First milestone

A working loop: Slack message → Central-Dispatch webhook → event log → WebSocket →
agent-wrapper → Claude Code → reply in Slack, with the agent workspace set up by hand
and registered via dial-home. Central-Dispatch runs with `npm start` (no broker, no
managed services); the agent-wrapper is a hand-run `claudebot` (installed via
`uv tool install`).
**Status: working end to end** (Central-Dispatch on Railway/behind a tunnel, agent-wrapper on a
laptop).
