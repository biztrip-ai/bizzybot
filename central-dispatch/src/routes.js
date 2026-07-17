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
import { pushEvent, onlineIds, claimOfflineNotice } from './wsHub.js';
import { getSession, setSession, clearSession } from './session.js';
import {
  SLACK_BOT_SCOPES,
  verifySlackSignature,
  exchangeCode,
  buildManifest,
  oidcAuthorizeUrl,
  exchangeOidcCode,
  decodeIdToken,
  postSlackMessage,
  botInThread,
} from './slack.js';

export const router = express.Router();

function fmtAgo(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.floor((Date.now() - Number(ts)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

// --- Landing ----------------------------------------------------------------
const btn = (href, label, bg = '#4A154B') =>
  `<a href="${href}" style="display:inline-block;background:${bg};color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;margin-right:8px">${label}</a>`;

router.get('/', (req, res) => {
  const canInstall = Boolean(config.slack.clientId);
  const sess = getSession(req);
  res.type('html').send(`<!doctype html><meta charset="utf-8">
<title>Claudebot</title>
<body style="font-family:system-ui;max-width:640px;margin:48px auto;padding:0 16px">
<h1>Claudebot</h1>
<p>A Slack-native AI teammate you self-host.</p>
${
  !canInstall
    ? '<p><em>Slack is not configured. Set SLACK_CLIENT_ID / SLACK_CLIENT_SECRET.</em></p>'
    : sess
      ? `<p>${btn('/dashboard', 'Open dashboard')}<a href="/logout">sign out</a></p>`
      : `<p>${btn('/login', 'Sign in with Slack')}</p>
<p style="color:#666;font-size:14px">Sign in with your Slack account first. Once you're in, you can add the Claudebot app to your workspace.</p>`
}
</body>`);
});

// --- Slack OAuth install ("Add to Slack") -----------------------------------
// CSRF states pending a callback (in-memory: single-process Central-Dispatch).
const pendingStates = new Map(); // state -> expiresAt

// --- Sign in with Slack (dashboard auth) ------------------------------------
router.get('/login', (req, res) => {
  if (!config.slack.clientId) return res.status(500).send('Slack not configured');
  const state = crypto.randomBytes(16).toString('hex');
  pendingStates.set(state, Date.now() + 10 * 60 * 1000);
  res.redirect(oidcAuthorizeUrl(state, `${config.publicUrl}/auth/slack/callback`));
});

router.get('/auth/slack/callback', async (req, res) => {
  const { code, state, error } = req.query;
  if (error) return res.status(400).send(`Slack error: ${escapeHtml(error)}`);
  if (!code) return res.status(400).send('missing code');
  const exp = pendingStates.get(state);
  pendingStates.delete(state);
  if (!exp || exp < Date.now()) return res.status(400).send('state mismatch or expired');

  const data = await exchangeOidcCode(code, `${config.publicUrl}/auth/slack/callback`);
  if (!data.ok || !data.id_token) {
    return res.status(400).send(`sign-in failed: ${escapeHtml(data.error || 'unknown')}`);
  }
  const id = decodeIdToken(data.id_token);
  if (!id || !id.teamId) return res.status(400).send('could not read Slack identity');

  setSession(res, { teamId: id.teamId, userId: id.userId, name: id.name, teamName: id.teamName });
  res.redirect('/dashboard');
});

router.get('/logout', (req, res) => {
  clearSession(res);
  res.redirect('/');
});

// Per-tenant dashboard: the signed-in workspace's own agent only.
router.get('/dashboard', async (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.redirect('/login');

  const agent = await getAgentByTeam(sess.teamId);
  const workspace = agent?.name || sess.teamName || sess.teamId;
  const online = agent ? onlineIds().has(agent.id) : false;

  const body = agent
    ? `<p>Slack: <b>✅ installed</b> · Agent-Wrapper: <b>${
        online ? '🟢 online' : `⚪️ offline · last seen ${fmtAgo(agent.last_seen_at)}`
      }</b></p>
       <h3>Connect your teammate</h3>
       <p>On the machine where the teammate should run, install it once:</p>
       <pre style="background:#f4f4f4;padding:12px;border-radius:6px;overflow:auto">uv tool install "git+https://github.com/biztrip-ai/claudebot.git#subdirectory=agent-wrapper"</pre>
       <p>Then start it with your registration token:</p>
       <pre style="background:#f4f4f4;padding:12px;border-radius:6px;overflow:auto">CENTRAL_URL=${escapeHtml(config.publicUrl)} REGISTRATION_TOKEN=${escapeHtml(agent.registration_token)} claudebot</pre>
       <p>Requires <code>claude</code> and <code>gh</code> on PATH. Keep the registration token secret.</p>`
    : `<p>No teammate is installed in this workspace yet.</p>
       <p>${btn('/slack/install', 'Add to Slack')}</p>`;

  res.type('html').send(`<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Claudebot — ${escapeHtml(workspace)}</title>
<body style="font-family:system-ui;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.5">
<p style="color:#666">Signed in as ${escapeHtml(sess.name || 'you')} · <a href="/logout">sign out</a></p>
<h1>${escapeHtml(workspace)}</h1>
${body}
</body>`);
});

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

  // Log the installer in and land them on the dashboard (which shows the token
  // + live status for this workspace).
  setSession(res, {
    teamId,
    userId: data.authed_user?.id ?? null,
    name: null,
    teamName,
  });
  res.redirect('/dashboard');
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
      const online = onlineIds();
      for (const a of targets) {
        const ev = await appendEvent(a.id, 'slack_event', req.body.event);
        pushEvent(a.id, ev);
      }
      // If the message is addressed to the bot but no agent is connected to
      // handle it (e.g. the agent-wrapper is restarting), post a one-off notice so the
      // user isn't left staring at silence. The event is still logged and will
      // be replayed to the agent when it reconnects.
      if (targets.length && !targets.some((a) => online.has(a.id))) {
        await notifyOfflineIfAddressed(teamId, targets, req.body.event);
      }
    } catch (e) {
      // Already acked to Slack; log and move on so it isn't an unhandled rejection.
      console.error('[slack events] fan-out failed:', e);
    }
  }
});

const _NOTIFY_IGNORED_SUBTYPES = new Set([
  'bot_message',
  'message_changed',
  'message_deleted',
  'channel_join',
]);

// Post an "agent offline" notice to Slack when a message clearly addressed to
// the bot arrives with no agent online to handle it. Best-effort and deduped
// per thread per offline episode (see claimOfflineNotice).
async function notifyOfflineIfAddressed(teamId, targets, event) {
  if (!event || typeof event !== 'object') return;
  if (event.bot_id || _NOTIFY_IGNORED_SUBTYPES.has(event.subtype)) return;

  const channel = event.channel;
  const ts = event.ts;
  if (!channel || !ts) return;
  const threadTs = event.thread_ts || ts;

  const agent = await getAgentByTeam(teamId);
  const token = agent && agent.slack_bot_token;
  if (!token) return; // can't post without the workspace bot token

  // Decide whether this event is actually aimed at the bot.
  const type = event.type;
  const channelType = event.channel_type;
  let addressed = false;
  if (type === 'app_mention') {
    addressed = true;
  } else if (type === 'message' && channelType === 'im') {
    addressed = true;
  } else if (
    type === 'message' &&
    event.thread_ts &&
    ['channel', 'group', 'mpim'].includes(channelType)
  ) {
    // A bare thread reply — only notify if the bot is actually in this thread,
    // so we don't butt into unrelated conversations. Runs only while offline.
    addressed = await botInThread({ token, channel, threadTs });
  }
  if (!addressed) return;

  if (!claimOfflineNotice(targets[0].id, `${channel}:${threadTs}`)) return;

  try {
    await postSlackMessage({
      token,
      channel,
      threadTs,
      text: ':zzz: The agent is offline right now (it may be restarting). Your message is saved — it will be picked up once the agent reconnects.',
    });
  } catch (e) {
    console.warn('[slack events] offline notice failed:', e.message);
  }
}
