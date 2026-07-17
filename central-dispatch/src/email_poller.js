// Inbound-email poller (see docs/EMAIL.md). One background loop polls the
// Mailgun Events API once per workspace that has Mailgun configured, routes each
// newly-stored message to the agent that owns its recipient local-part, and
// appends an 'email' event (carrying the workspace's Mailgun creds for the
// reply) that the agent-wrapper turns into a Claude turn.
//
// Talks to Mailgun over plain fetch with HTTP Basic auth (api:<key>). Dedup is
// by Message-Id (falling back to the Mailgun event id), in-memory and global.
// On startup we seed — record everything currently stored as already-seen — so
// a restart/redeploy doesn't re-handle old mail (the trade-off: mail that lands
// during the restart window is seeded-as-seen and never handled).
import {
  listConfiguredWorkspaces,
  getAgentByEmailLocalPart,
  appendEvent,
} from './store.js';
import { pushEvent } from './wsHub.js';

const DEFAULT_BASE_URL = 'https://api.mailgun.net';
const FETCH_LIMIT = 50; // most-recent events per poll; Mailgun returns newest-first
const MAX_BODY_CHARS = 6000; // cap the body handed to Claude

const POLL_INTERVAL_S = Number(process.env.EMAIL_POLL_INTERVAL_S || 60);
// Instance-wide last-resort sender allow-list (comma-separated domains), for
// single-org self-hosting. Normally the allow-list is set per-workspace (and
// optionally per-agent) in the dashboard. If none of the three name a domain,
// the agent is closed — no inbound mail fires (see docs/EMAIL.md).
const ENV_SENDER_ALLOW = process.env.EMAIL_SENDER_DOMAIN || '';

const seen = new Set(); // Message-Ids already handled/seeded

function authHeader(apiKey) {
  return 'Basic ' + Buffer.from(`api:${apiKey}`).toString('base64');
}

// Bare, lowercased address from a "Name <addr@dom>" or plain "addr@dom" string.
function bareAddr(raw) {
  if (!raw) return '';
  const m = String(raw).match(/<([^>]+)>/);
  return (m ? m[1] : raw).trim().toLowerCase();
}

function domainOf(addr) {
  return addr.split('@')[1] || '';
}

function parseAllow(csv) {
  return new Set(
    String(csv || '')
      .split(',')
      .map((s) => s.trim().toLowerCase().replace(/^@/, ''))
      .filter(Boolean),
  );
}

// List recently-stored inbound messages for one workspace (metadata only).
async function fetchStored(ws) {
  const base = (ws.mailgun_base_url || DEFAULT_BASE_URL).replace(/\/$/, '');
  const url = `${base}/v3/${ws.mailgun_domain}/events?event=stored&limit=${FETCH_LIMIT}&ascending=no`;
  let resp;
  try {
    resp = await fetch(url, { headers: { Authorization: authHeader(ws.mailgun_api_key) } });
  } catch (e) {
    console.warn(`[email] events fetch failed for ${ws.mailgun_domain}:`, e.message);
    return [];
  }
  if (!resp.ok) {
    const body = (await resp.text().catch(() => '')).slice(0, 300);
    console.warn(`[email] events fetch ${resp.status} for ${ws.mailgun_domain}: ${body}`);
    return [];
  }
  const data = await resp.json().catch(() => ({}));
  const out = [];
  for (const it of data.items || []) {
    const headers = (it.message && it.message.headers) || {};
    const fromRaw = headers.from || '';
    const messageId = headers['message-id'] || it.id || '';
    if (!messageId) continue;
    let recipient = it.recipient || '';
    if (!recipient) recipient = bareAddr(headers.to || '');
    out.push({
      messageId,
      fromRaw,
      fromAddr: bareAddr(fromRaw),
      subject: headers.subject || '',
      timestamp: Number(it.timestamp || 0),
      storageUrl: (it.storage && it.storage.url) || '',
      recipient: recipient.toLowerCase(),
    });
  }
  return out;
}

// Fetch the parsed body from Mailgun storage. Best-effort: empty on any failure.
async function fetchBody(ws, storageUrl) {
  if (!storageUrl) return '';
  let resp;
  try {
    resp = await fetch(storageUrl, {
      headers: { Authorization: authHeader(ws.mailgun_api_key), Accept: 'application/json' },
    });
  } catch (e) {
    console.warn('[email] storage fetch failed:', e.message);
    return '';
  }
  if (!resp.ok) {
    console.warn(`[email] storage fetch ${resp.status}`);
    return '';
  }
  const data = await resp.json().catch(() => ({}));
  let body = data['stripped-text'] || data['body-plain'] || '';
  if (body.length > MAX_BODY_CHARS) body = body.slice(0, MAX_BODY_CHARS) + '\n…[truncated]';
  return body.trim();
}

// One poll of one workspace: route+fire any new, allowed messages to their agent.
async function pollWorkspace(ws, { seeding }) {
  const msgs = await fetchStored(ws);
  // Oldest-first so threads open in arrival order.
  const fresh = msgs.filter((m) => !seen.has(m.messageId)).sort((a, b) => a.timestamp - b.timestamp);
  for (const m of fresh) {
    seen.add(m.messageId); // record every new message so we never re-fire
    if (seeding) continue;

    const localPart = m.recipient.split('@')[0];
    let agent;
    try {
      agent = await getAgentByEmailLocalPart(ws.team_id, localPart);
    } catch (e) {
      console.error('[email] agent lookup failed:', e);
      continue;
    }
    if (!agent || !agent.email_channel) continue; // no agent owns this mailbox

    // Sender allow-list is the trust boundary (inbound mail is attacker-
    // controlled). Precedence: per-agent override → workspace default → env
    // fallback. Default closed: if none names a domain, nothing fires.
    const allow = parseAllow(
      agent.email_sender_allow || ws.sender_allow || ENV_SENDER_ALLOW,
    );
    if (allow.size === 0 || !allow.has(domainOf(m.fromAddr))) {
      console.log(
        `[email] skipping ${m.fromAddr || '?'} → ${m.recipient} (sender not allowed)`,
      );
      continue;
    }

    const body = await fetchBody(ws, m.storageUrl);
    const payload = {
      message_id: m.messageId,
      from_raw: m.fromRaw,
      from_addr: m.fromAddr,
      subject: m.subject,
      body,
      recipient: m.recipient,
      timestamp: m.timestamp,
      channel: agent.email_channel,
      mailgun: {
        domain: ws.mailgun_domain,
        apiKey: ws.mailgun_api_key,
        baseUrl: ws.mailgun_base_url || DEFAULT_BASE_URL,
      },
    };
    try {
      const ev = await appendEvent(agent.id, 'email', payload);
      pushEvent(agent.id, ev);
      console.log(
        `[email] routed ${m.fromAddr} → agent ${agent.id} (${m.recipient}) subject=${JSON.stringify(m.subject)}`,
      );
    } catch (e) {
      console.error('[email] append/push failed:', e);
    }
  }
}

// Exported for tests; called internally by the loop below.
export async function pollOnce(opts) {
  let workspaces = [];
  try {
    workspaces = await listConfiguredWorkspaces();
  } catch (e) {
    console.error('[email] listing workspaces failed:', e);
    return;
  }
  for (const ws of workspaces) {
    try {
      await pollWorkspace(ws, opts);
    } catch (e) {
      console.error(`[email] poll failed for ${ws.mailgun_domain}:`, e);
    }
  }
}

// Start the poller. Returns a stop() to cancel the loop. Idempotent enough for
// a single call from index.js.
export function startEmailPoller() {
  let stopped = false;
  let timer = null;

  (async () => {
    // Seed once (record current mail as seen, fire nothing), then loop.
    await pollOnce({ seeding: true });
    console.log(
      `[email] poller started: interval=${POLL_INTERVAL_S}s, seeded ${seen.size} message(s)`,
    );
    const tick = async () => {
      if (stopped) return;
      await pollOnce({ seeding: false });
      if (!stopped) timer = setTimeout(tick, POLL_INTERVAL_S * 1000);
    };
    timer = setTimeout(tick, POLL_INTERVAL_S * 1000);
  })().catch((e) => console.error('[email] poller startup failed:', e));

  return {
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
    },
  };
}
