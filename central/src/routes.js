import express from 'express';
import crypto from 'node:crypto';
import { config } from './config.js';
import {
  createAgent,
  getAgentByToken,
  getAgentByTeam,
  setAgentSlack,
  markRegistered,
  listAgents,
  appendEvent,
} from './store.js';
import { pushEvent } from './wsHub.js';
import {
  SLACK_BOT_SCOPES,
  verifySlackSignature,
  exchangeCode,
  buildManifest,
} from './slack.js';

export const router = express.Router();

function requireAdmin(req, res) {
  if (!config.adminKey || req.get('x-admin-key') !== config.adminKey) {
    res.status(401).json({ error: 'unauthorized' });
    return false;
  }
  return true;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

// --- Landing ----------------------------------------------------------------
router.get('/', (_req, res) => {
  const canInstall = Boolean(config.slack.clientId);
  res.type('html').send(`<!doctype html><meta charset="utf-8">
<title>Claudebot</title>
<body style="font-family:system-ui;max-width:640px;margin:48px auto;padding:0 16px">
<h1>Claudebot</h1>
<p>A Slack-native AI teammate you self-host.</p>
${
  canInstall
    ? '<p><a href="/slack/install" style="display:inline-block;background:#4A154B;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none">Add to Slack</a></p>'
    : '<p><em>Slack is not configured. Set SLACK_CLIENT_ID / SLACK_CLIENT_SECRET.</em></p>'
}
</body>`);
});

// --- Admin ------------------------------------------------------------------
// Stand-in for the Slack sign-in flow: creates an agent and returns a
// registration token for the operator to paste into the bridge.
router.post('/api/admin/agents', async (req, res) => {
  if (!requireAdmin(req, res)) return;
  const name = (req.body && req.body.name) || 'agent';
  res.json(await createAgent(name));
});

router.get('/api/admin/agents', async (req, res) => {
  if (!requireAdmin(req, res)) return;
  res.json(await listAgents());
});

// --- Slack OAuth install ("Add to Slack") -----------------------------------
// CSRF states pending a callback (in-memory: single-process Central).
const pendingStates = new Map(); // state -> expiresAt

router.get('/slack/install', (req, res) => {
  if (!config.slack.clientId) {
    return res.status(500).send('SLACK_CLIENT_ID not configured');
  }
  const state = crypto.randomBytes(16).toString('hex');
  pendingStates.set(state, Date.now() + 10 * 60 * 1000);

  const u = new URL('https://slack.com/oauth/v2/authorize');
  u.searchParams.set('client_id', config.slack.clientId);
  u.searchParams.set('scope', SLACK_BOT_SCOPES.join(','));
  u.searchParams.set('redirect_uri', `${config.publicUrl}/slack/oauth/callback`);
  u.searchParams.set('state', state);
  res.redirect(u.toString());
});

router.get('/slack/oauth/callback', async (req, res) => {
  const { code, state, error } = req.query;
  if (error) return res.status(400).send(`Slack error: ${escapeHtml(error)}`);
  if (!code) return res.status(400).send('missing code');

  const exp = pendingStates.get(state);
  pendingStates.delete(state);
  if (!exp || exp < Date.now()) return res.status(400).send('state mismatch or expired');

  const data = await exchangeCode(code, `${config.publicUrl}/slack/oauth/callback`);
  if (!data.ok || !data.access_token) {
    return res.status(400).send(`token exchange failed: ${escapeHtml(data.error || 'unknown')}`);
  }

  const teamId = data.team?.id ?? null;
  const teamName = data.team?.name ?? 'your workspace';
  const botToken = data.access_token;

  // Reuse the workspace's existing agent (re-auth) or create a new one.
  let registrationToken;
  const existing = teamId ? await getAgentByTeam(teamId) : null;
  if (existing) {
    await setAgentSlack(existing.id, { teamId, botToken });
    registrationToken = existing.registration_token;
  } else {
    const agent = await createAgent(teamName);
    await setAgentSlack(agent.id, { teamId, botToken });
    registrationToken = agent.registrationToken;
  }

  res.type('html').send(`<!doctype html><meta charset="utf-8">
<title>Claudebot — connected</title>
<body style="font-family:system-ui;max-width:680px;margin:48px auto;padding:0 16px;line-height:1.5">
<h1>✅ Connected to ${escapeHtml(teamName)}</h1>
<p>Now start your teammate on the machine where it should run (laptop, VM, container):</p>
<ol>
<li>Configure the bridge:
<pre style="background:#f4f4f4;padding:12px;border-radius:6px;overflow:auto">CENTRAL_URL=${escapeHtml(config.publicUrl)}
REGISTRATION_TOKEN=${escapeHtml(registrationToken)}</pre></li>
<li>Run it: <pre style="background:#f4f4f4;padding:12px;border-radius:6px">uv run python bridge.py</pre></li>
</ol>
<p>Keep the registration token secret — anyone with it can connect a teammate to your workspace.
Once the bridge is online, <b>@mention the bot</b> in Slack or send it a DM.</p>
</body>`);
});

// --- Slack app manifest -----------------------------------------------------
router.get('/slack/manifest', (req, res) => {
  const name = req.query.name || config.slack.appName;
  res.json(buildManifest({ appName: name, baseUrl: config.publicUrl }));
});

// --- Registration (dial-home) ----------------------------------------------
router.post('/api/register', async (req, res) => {
  const token = req.body && req.body.token;
  const agent = token ? await getAgentByToken(token) : null;
  if (!agent) return res.status(401).json({ error: 'invalid registration token' });

  // Just stamp registered_at — do NOT touch slack_bot_token/team, which were set
  // per-workspace during OAuth. (Passing an empty env token here would clobber
  // the tenant's real bot token via COALESCE.)
  await markRegistered(agent.id);

  const wsUrl = config.publicUrl.replace(/^http/, 'ws') + '/ws';
  res.json({
    agentId: agent.id,
    slackBotToken: agent.slack_bot_token || config.slack.botToken,
    ws: { url: wsUrl, token },
  });
});

// --- Slack events webhook ---------------------------------------------------
router.post('/slack/events', async (req, res) => {
  if (req.body && req.body.type === 'url_verification') {
    return res.json({ challenge: req.body.challenge });
  }

  const verified = verifySlackSignature({
    signingSecret: config.slack.signingSecret,
    signature: req.get('x-slack-signature'),
    timestamp: req.get('x-slack-request-timestamp'),
    rawBody: req.rawBody || '',
  });
  if (!verified) return res.status(401).send('bad signature');

  // Ack fast (Slack needs a response < 3s), then fan out to agents.
  res.sendStatus(200);

  if (req.body && req.body.type === 'event_callback') {
    const teamId = req.body.team_id || null;
    // Multi-tenant isolation: deliver ONLY to agents bound to this exact Slack
    // workspace. Never broadcast — that would leak one tenant's events to
    // another. (A dev/admin agent with a null team only matches team-less test
    // events, never real workspace traffic.)
    try {
      const targets = (await listAgents()).filter((a) => a.slack_team_id === teamId);
      for (const a of targets) {
        const ev = await appendEvent(a.id, 'slack_event', req.body.event);
        pushEvent(a.id, ev);
      }
    } catch (e) {
      // Already acked to Slack; log and move on so it isn't an unhandled rejection.
      console.error('[slack events] fan-out failed:', e);
    }
  }
});
