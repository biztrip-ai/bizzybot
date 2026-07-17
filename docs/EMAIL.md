# Inbound Email — Design Spec

Status: **spec / not yet built**. Ported from the old monolith
(`claude-slack-bot/email_poller.py` + `mailgun_reply.py`) to the split
Central-Dispatch / agent-wrapper architecture.

## Goal

Let an agent receive inbound email and act on it: when mail arrives for an
agent, open a Slack thread, hand the email to Claude, and reply by email.
Reproduces the old behavior, fitted to the two-process design and made
multi-tenant.

## Why this spans both components

In the old monolith one process ran the Slack bot *and* Claude, so the poller
called `handle_user_message()` directly. Now:

- **Central-Dispatch** (Node, Railway) — detects inbound email, routes it to the
  right agent, emits a durable per-agent event over the `/ws` WebSocket. Does
  **not** run Claude.
- **agent-wrapper** (Python, user's machine) — the only thing that drives Claude
  and sends the reply.

Central owns *ingestion + routing + credentials*; the agent-wrapper owns
*handling + sending*. Both need changes.

## Decisions locked

1. **Ingestion: polling.** Central runs a background loop against the Mailgun
   Events API (`event=stored`), like the old `EmailPoller` — seed-on-start,
   dedup by `Message-Id`. Not a webhook.
2. **Tenancy: per-workspace Mailgun, per-agent addresses.** Each **workspace**
   configures its own Mailgun domain + API key in the dashboard. Within a
   workspace, each **agent** has its own inbound local-part. Central routes each
   polled email to an agent by its `recipient` (delivered-to) address.
3. **Reply credentials: workspace-owned, delivered in the event payload, agent
   sends.** The workspace's Mailgun key lives in Central. For an auto-reply,
   Central includes the key in the `email` event payload; the agent uses it in
   memory to send that one reply and never persists it to disk/config. The key
   **never enters Claude's environment** (see Security). It does transit
   Central's durable event log until the agent acks (pruned on ack; dropped by
   the 10-min staleness sweep) — an accepted simplicity trade-off. General
   Mailgun tooling for Claude is a separate opt-in (a key the user puts in the
   agent's `settings.env`).

### Config split

- **Central owns:** per-workspace Mailgun domain + key (dashboard → DB);
  per-agent inbound local-part, Slack channel, and optional sender allow-list
  (dashboard → DB); global poll interval + default sender domain (env).
- **agent-wrapper owns:** nothing required for the auto-reply — it receives the
  transient key from Central. *Optionally* a standing `MAILGUN_API_KEY` in
  `settings.env` if the user wants Claude to send arbitrary mail via Bash.

## Flow

```
                    ┌──────────────────── Central-Dispatch ───────────────────────┐
Mailgun (stored) ──▶│ EmailPoller — one pass per workspace that has Mailgun set   │
                    │   fetch <domain>/events with that workspace's key           │
                    │   dedup by Message-Id                                        │
                    │   map recipient local-part → agent (agents.email_local_part)│
                    │   sender allow-list check                                    │
                    │   appendEvent(agentId,'email',{…+transient key…}); pushEvent │
                    └───────────────────────────────┬─────────────────────────────┘
                                                     │ ws frame {type:'event',
                                                     │   event:{type:'email', payload}}
                    ┌──────────────── agent-wrapper ┼──────────────────────────────┐
                    │ consume(): branch on event.type                              │
                    │   'email' → handle_email_event(payload):                     │
                    │     post announce to payload.channel → thread                │
                    │     run a Claude turn: compose reply in a delimited block    │
                    │       (Claude has NO Mailgun key)                            │
                    │     agent sends reply in-process with payload.mailgun creds: │
                    │       From=recipient, In-Reply-To=message_id ; drops creds   │
                    └───────────────────────────────────────────────────────────────┘
```

## Data model (Central)

**New `workspace_settings` table** (keyed by `team_id`; there is no workspace
table today — agents are keyed by team+app):

| column            | type | meaning |
|-------------------|------|---------|
| `team_id`         | TEXT PRIMARY KEY | Slack workspace |
| `mailgun_domain`  | TEXT | receiving + sending domain |
| `mailgun_api_key` | TEXT | account-wide key (receive via Events API, send replies) |
| `mailgun_base_url`| TEXT | optional; `https://api.eu.mailgun.net` for EU |

**Add to `agents`** (idempotent migration, matching the existing
`ADD COLUMN IF NOT EXISTS` / PRAGMA pattern in `db.js`):

| column               | type | meaning |
|----------------------|------|---------|
| `email_local_part`   | TEXT UNIQUE | inbound mailbox, e.g. `bizzy` → `bizzy@<domain>`. Null ⇒ email off. |
| `email_channel`      | TEXT | Slack channel for email threads. |
| `email_sender_allow` | TEXT | optional comma-separated allowed sender domains; falls back to `EMAIL_SENDER_DOMAIN`. |

Email is enabled for an agent iff its workspace has Mailgun configured **and**
the agent has `email_local_part` + `email_channel`.

New store.js helpers: `getWorkspaceSettings(teamId)`,
`setWorkspaceSettings(teamId, {...})`, `getAgentByEmailLocalPart(teamId,
localPart)`, `setAgentEmail(id, {...})`, and email fields in `listAgentsByTeam`.

## Central components

- **`src/email_poller.js`** — background loop. Each interval, for every
  workspace with Mailgun configured:
  - GET `<base>/v3/<domain>/events?event=stored&limit=50&ascending=no`, HTTP
    Basic (`api:<key>`). Parse → `{message_id, from_addr, subject, timestamp,
    storage_url, recipient}`.
  - Seed on start (record current Message-Ids as seen, don't fire) so a Railway
    redeploy doesn't re-handle old mail. **Tradeoff:** mail arriving during the
    restart window is seeded-as-seen and never handled — same as the old design.
    Seen-set is in-memory, global, keyed by Message-Id.
  - Per new email: dedup → `getAgentByEmailLocalPart(teamId, localPart)` → sender
    allow-list check → `_fetch_body()` from `storage_url` (cap ~6000 chars) →
    `appendEvent(agentId, 'email', payload)` + `pushEvent`. Payload carries the
    workspace's Mailgun creds for the reply (see payload shape).
  - No matching agent / disallowed sender → record-as-seen and skip (debug log).
- **`src/routes.js`** —
  - Dashboard: a **workspace Mailgun section** (domain + key + base url) and, per
    agent card, `email_local_part` (shows full address), `email_channel`, and
    optional sender allow-list; `POST` routes to save both.
- **`index.js`** — start the poller after `init()` (guarded: only workspaces
  with Mailgun configured are polled; loop is a no-op if none).

## Agent-wrapper components

- **Event-type routing** — today `consume()` passes only `event.payload` to
  `dispatch_event`, ignoring `event.type`. Thread the type through and branch:
  `slack_event` → existing path; `email` → `handle_email_event(payload)`.
- **`handle_email_event(payload)`** —
  1. post an announcement (`📧 New email from … / subject / snippet`) to
     `payload.channel` → `parent_ts`.
  2. run a Claude turn (streamed to that thread) whose instruction includes the
     email and asks Claude to **compose** a reply inside a delimited block —
     analogous to the existing `ATTACH:` convention. Claude has **no** Mailgun
     key and does **not** send.
  3. if the turn produced a reply block: send the reply in-process (Python) with
     the payload's Mailgun creds — `From = recipient`, `To = from_addr`,
     `In-Reply-To`/`References = message_id` — then drop the creds. No block ⇒ no
     email sent (Claude chose not to reply / flagged for a human).
- **Reply sender** — a small in-process Mailgun `messages` POST (port
  `mailgun_reply.py`'s logic). The agent sets From/To/threading — Claude cannot
  influence recipients.
- **Optional standing tooling** — if `MAILGUN_API_KEY` is set in `settings.env`,
  it's exposed to Claude's env as usual, letting Claude send arbitrary mail via
  Bash. Separate and opt-in; unrelated to the transient auto-reply key.

## `email` event payload

```json
{
  "message_id": "<abc@biztrip.ai>",
  "from_raw":   "Alice <alice@biztrip.ai>",
  "from_addr":  "alice@biztrip.ai",
  "subject":    "Re: trip",
  "body":       "…(≤6000 chars, may be empty)…",
  "recipient":  "bizzy@mail.biztrip.ai",
  "timestamp":  1737000000.0,
  "channel":    "C0123456789",
  "mailgun":    { "domain": "mail.biztrip.ai", "apiKey": "key-…", "baseUrl": "https://api.mailgun.net" }
}
```

`recipient` is the reply `From`; `channel` is the routed agent's `email_channel`.
`mailgun` carries the workspace's sending creds — used by the agent in memory,
never persisted; it lives in the durable event log only until the agent acks.

## Configuration

**Central (env):**

| var | purpose |
|-----|---------|
| `EMAIL_POLL_INTERVAL_S` | poll cadence, default 60 |
| `EMAIL_SENDER_DOMAIN` | default sender allow-list when an agent sets none |

Mailgun domain/key/base-url are **per-workspace in the DB (dashboard)**, not env.

**agent-wrapper (`settings.env`, optional):** `MAILGUN_API_KEY`,
`MAILGUN_DOMAIN`, `MAILGUN_BASE_URL` — only if the user wants Claude to have a
standing send capability. Not needed for the auto-reply.

## Mailgun setup (per workspace)

1. A **catch-all inbound Route** with a `store()` action, so all `*@<domain>`
   mail is retained (~3 days) and appears in the Events API. Central filters by
   recipient — no per-agent routes.
2. Sending domain verified for replies (same key).

## Security

- **Sender allow-list is the trust boundary.** Inbound mail is attacker-
  controlled and is fed to Claude in `bypassPermissions`. Only fire for senders
  on the agent's allow-list (or the global default). An agent with no allow-list
  configured defaults to **closed**, not open.
- **The key never enters Claude's environment.** Because the email body can
  carry prompt-injection, Claude composes but does not send; the agent-wrapper
  performs the Mailgun call with the payload's creds, held in memory and never
  written to disk/config. The agent (not Claude) sets recipient/threading, so an
  injection can't redirect the reply or spam via the key. (The key does sit in
  Central's event log until ack — see decision 3.)
- **Workspace keys at rest.** Stored in Central's DB like `slack_bot_token`
  (plaintext today). A Mailgun key grants send+receive on the customer's domain,
  so encryption-at-rest is a recommended follow-up (not an MVP blocker).
- Treat the email body as untrusted data in the instruction template.

## Work breakdown

**Central:**
- [ ] `db.js`: `workspace_settings` table; `email_local_part` / `email_channel` / `email_sender_allow` columns + migrations.
- [ ] `store.js`: workspace-settings + agent-email helpers; `getAgentByEmailLocalPart`; email fields in `listAgentsByTeam`.
- [ ] `email_poller.js`: per-workspace poll (fetch, seed, dedup, body, route, append+push).
- [ ] `routes.js`: dashboard Mailgun section + per-agent email fields + save routes.
- [ ] `index.js`: start the poller.
- [ ] `.env.example`: `EMAIL_POLL_INTERVAL_S`, `EMAIL_SENDER_DOMAIN`.

**agent-wrapper:**
- [ ] Thread `event.type` through `consume` → `dispatch_event`.
- [ ] `handle_email_event()` + instruction template + reply-block convention.
- [ ] In-process Mailgun sender using the payload's creds.
- [ ] `settings.env.example`: document the *optional* standing-capability key.

**Docs / ops:**
- [ ] Mailgun catch-all store Route + verified sending domain (per workspace).
- [ ] README: brief "receiving email" section.

## Open sub-questions

1. **Address scheme.** User-chosen local-part per agent (friendly, needs
   uniqueness + a dashboard field) — the plan above. Alternative: deterministic
   `agent-<id>@domain` (no UI, ugly). Recommend user-chosen.
2. **Persisted dedup?** In-memory seen-set re-seeds on redeploy (can miss
   in-window mail). Fine for MVP; a `seen_email` table closes the gap later.
3. **Credential delivery.** RESOLVED: the key is delivered in the `email` event
   payload (simplest). It sits in the events table only until the agent acks
   (then pruned) and is dropped by the 10-min staleness sweep; the agent never
   persists it and Claude never sees it.

## Out of scope (MVP)

Attachments, HTML-body rendering, multi-email Slack threading, encryption-at-
rest of stored Mailgun keys, and a Central-side send endpoint (the agent sends).
