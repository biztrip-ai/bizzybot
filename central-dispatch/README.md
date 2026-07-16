# Central-Dispatch

Vanilla web app + event dispatcher. Express + `ws` + built-in `node:sqlite`.

## Run

```bash
cp .env.example .env   # edit as needed
npm start              # or: npm run dev  (node --watch)
```

## Endpoints

| Method | Path                  | Purpose                                                        |
|--------|-----------------------|----------------------------------------------------------------|
| GET    | `/`                   | Landing page with an "Add to Slack" button.                    |
| GET    | `/health`             | Liveness check.                                                |
| GET    | `/slack/install`      | Start the Slack OAuth flow ("Add to Slack").                   |
| GET    | `/slack/oauth/callback` | OAuth redirect; creates the agent and shows the registration token. |
| GET    | `/slack/manifest`     | Slack app manifest JSON (create the app from this once).       |
| POST   | `/slack/events`       | Slack Events API webhook.                                       |
| POST   | `/api/admin/agents`   | Create an agent, get a registration token. Needs `x-admin-key`.|
| GET    | `/api/admin/agents`   | List agents. Needs `x-admin-key`.                              |
| POST   | `/api/register`       | Agent-Wrapper dials home with its registration token.                 |
| WS     | `/ws?token=<regtok>&lastSeq=<n>` | Agent event stream (replay + live push).            |

## Slack setup

1. `GET /slack/manifest` → create a Slack app from the manifest.
2. Copy the app's **Signing Secret**, **Client ID**, **Client Secret** into
   `.env` (`SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`).
3. Visit `/` → **Add to Slack** → authorize. Central-Dispatch shows you a **registration
   token** and the exact agent-wrapper config to paste in.

## How it works

1. **Create an agent** (stands in for the Slack sign-in step):

   ```bash
   curl -s -X POST localhost:3000/api/admin/agents \
     -H "x-admin-key: change-me" -H "content-type: application/json" \
     -d '{"name":"cosmo"}'
   # -> { "id": "...", "name": "cosmo", "registrationToken": "..." }
   ```

2. **Register** (what the agent-wrapper does with that token):

   ```bash
   curl -s -X POST localhost:3000/api/register \
     -H "content-type: application/json" \
     -d '{"token":"<registrationToken>"}'
   # -> { "agentId": "...", "slackBotToken": "...", "ws": { "url": "...", "token": "..." } }
   ```

3. **Connect the WebSocket** to `ws.url?token=<regtok>&lastSeq=<n>`. Central-Dispatch
   replays events with `seq > lastSeq`, then live-pushes new ones. The client
   acks with `{ "type": "ack", "seq": <n> }`.

Inbound Slack events are appended to a per-agent, append-only log (durable), so
nothing is lost while an agent is offline — it catches up on reconnect.
