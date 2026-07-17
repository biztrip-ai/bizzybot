import express from 'express';
import crypto from 'node:crypto';
import { config } from './config.js';
import {
  createAgent,
  getAgentByToken,
  getAgentByTeam,
  getAgentByTeamAndApp,
  getAgentById,
  listAgentsByTeam,
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

// Source repo — linked from the dashboard, and where the install command below
// pulls the agent-wrapper from.
const REPO_URL = 'https://github.com/biztrip-ai/bizzybot';

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
<title>Bizzybot</title>
<body style="font-family:system-ui;max-width:640px;margin:48px auto;padding:0 16px">
<h1>Bizzybot</h1>
<p>A Slack-native AI teammate you self-host.</p>
${
  !canInstall
    ? '<p><em>Slack is not configured. Set SLACK_CLIENT_ID / SLACK_CLIENT_SECRET.</em></p>'
    : sess
      ? `<p>${btn('/dashboard', 'Open dashboard')}<a href="/logout">sign out</a></p>`
      : `<p>${btn('/login', 'Sign in with Slack')}</p>
<p style="color:#666;font-size:14px">Sign in with your Slack account first. Once you're in, you can add the Bizzybot app to your workspace.</p>`
}
</body>`);
});

// --- Slack OAuth install ("Add to Slack") -----------------------------------
// CSRF states pending a callback (in-memory: single-process Central-Dispatch).
// Value: { expiresAt, appId? } — appId records which Slack app an install is for.
const pendingStates = new Map();

function newState(extra = {}) {
  const state = crypto.randomBytes(16).toString('hex');
  pendingStates.set(state, { expiresAt: Date.now() + 10 * 60 * 1000, ...extra });
  return state;
}

// Consume a pending state, returning it iff present and unexpired.
function takeState(state) {
  const pending = pendingStates.get(state);
  pendingStates.delete(state);
  if (!pending || pending.expiresAt < Date.now()) return null;
  return pending;
}

// --- Sign in with Slack (dashboard auth) ------------------------------------
// Sign-in is workspace identity, so any configured app works — use the primary.
router.get('/login', (req, res) => {
  if (!config.slack.clientId) return res.status(500).send('Slack not configured');
  const state = newState();
  res.redirect(oidcAuthorizeUrl(state, `${config.publicUrl}/auth/slack/callback`));
});

router.get('/auth/slack/callback', async (req, res) => {
  const { code, state, error } = req.query;
  if (error) return res.status(400).send(`Slack error: ${escapeHtml(error)}`);
  if (!code) return res.status(400).send('missing code');
  if (!takeState(state)) return res.status(400).send('state mismatch or expired');

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

// Per-tenant dashboard: every agent in the signed-in workspace, one card per
// configured Slack app (a workspace may run several cloned apps / agents).
router.get('/dashboard', async (req, res) => {
  const sess = getSession(req);
  if (!sess) return res.redirect('/login');

  const workspace = sess.teamName || sess.teamId;
  const agents = await listAgentsByTeam(sess.teamId);
  const online = onlineIds();
  const apps = config.slack.apps;
  const multi = apps.length > 1;

  // Match each configured app to its installed agent. With a single app we bind
  // to whatever agent exists (legacy agents have no slack_app_id); otherwise we
  // match strictly by App ID.
  const agentForApp = (app) =>
    (multi
      ? agents.find((a) => a.slack_app_id && a.slack_app_id === app.appId)
      : agents[0]) || null;

  const preStyle = 'background:#f4f4f4;padding:12px;border-radius:6px;overflow:auto';
  const cardStyle =
    'border:1px solid #e5e5e5;border-radius:10px;padding:16px 18px;margin:16px 0';

  const installUrl = (app) =>
    app.appId ? `/slack/install?app=${encodeURIComponent(app.appId)}` : '/slack/install';

  const agentCard = (app, agent, i) => {
    const label = escapeHtml(agent?.name || app.name || 'Agent');
    if (!agent) {
      return `<div style="${cardStyle}">
        <h3 style="margin:0 0 8px">${label}</h3>
        <p style="margin:0 0 12px;color:#666">Not installed in this workspace yet.</p>
        ${btn(installUrl(app), 'Add to Slack')}
      </div>`;
    }
    const status = online.has(agent.id)
      ? '🟢 online'
      : `⚪️ offline · last seen ${fmtAgo(agent.last_seen_at)}`;
    const tokId = `regtok-${i}`;
    return `<div style="${cardStyle}">
      <h3 style="margin:0 0 8px">${label}</h3>
      <p style="margin:0 0 12px">Slack: <b>✅ installed</b> · Agent: <b>${status}</b></p>
      <p style="margin:0 0 6px">Registration token — paste it on first run of this agent:</p>
      <div style="display:flex;gap:8px;align-items:center;max-width:520px">
        <input id="${tokId}" value="${escapeHtml(agent.registration_token)}" readonly
          onclick="this.select()"
          style="flex:1;font-family:ui-monospace,SFMono-Regular,monospace;font-size:13px;padding:8px 10px;border:1px solid #ccc;border-radius:6px;background:#f9f9f9">
        <button type="button" onclick="copyTok('${tokId}',this)"
          style="padding:8px 14px;border:0;border-radius:6px;background:#4A154B;color:#fff;cursor:pointer;white-space:nowrap">Copy</button>
      </div>
    </div>`;
  };

  const cards = apps.map((app, i) => agentCard(app, agentForApp(app), i)).join('');
  // Surface any installed agents that don't correspond to a configured app
  // (e.g. an app removed from SLACK_APPS), so their tokens/status aren't hidden.
  const matched = new Set(apps.map(agentForApp).filter(Boolean).map((a) => a.id));
  const orphans = agents.filter((a) => !matched.has(a.id));
  const orphanCards = orphans
    .map((a, i) => agentCard({ name: a.name, appId: a.slack_app_id }, a, apps.length + i))
    .join('');

  res.type('html').send(`<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Bizzybot — ${escapeHtml(workspace)}</title>
<body style="font-family:system-ui;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.5">
<p style="color:#666;display:flex;gap:8px;align-items:baseline">
  <span>Signed in as ${escapeHtml(sess.name || 'you')} · <a href="/logout">sign out</a></span>
  <a href="${REPO_URL}" target="_blank" rel="noopener" style="margin-left:auto;color:#4A154B;text-decoration:none;white-space:nowrap">GitHub ↗</a>
</p>
<h1>${escapeHtml(workspace)}</h1>
<p style="color:#666">${
    multi ? `Run up to ${apps.length} agents in this workspace — one per Slack app.` : ''
  }</p>
${cards}${orphanCards}
<h3>Install an agent</h3>
<p>On the machine where an agent should run, install the wrapper once:</p>
<pre style="${preStyle}">uv tool install "git+${REPO_URL}.git#subdirectory=agent-wrapper"</pre>
<p>Then start it, once per agent, pasting that agent's registration token when prompted:</p>
<pre style="${preStyle}">bizzybot</pre>
<p style="color:#666;font-size:14px">Requires <code>claude</code> and <code>gh</code> on PATH. Keep registration tokens secret. Run each agent in its own directory (<code>BIZZYBOT_STATE_DIR</code>) so they don't share state.</p>
<script>
function copyTok(id,btn){
  var el=document.getElementById(id);
  if(!el) return;
  el.focus(); el.select();
  var done=function(){ var t=btn.textContent; btn.textContent='Copied!'; setTimeout(function(){btn.textContent=t;},1200); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(el.value).then(done).catch(function(){ try{document.execCommand('copy'); done();}catch(e){} });
  } else { try{document.execCommand('copy'); done();}catch(e){} }
}
</script>
</body>`);
});

router.get('/slack/install', (req, res) => {
  // Which app to install (?app=<App ID>); default to the primary app.
  const app = config.slack.appById(req.query.app) || config.slack.primary;
  if (!app.clientId) {
    return res.status(500).send('Slack client credentials not configured for this app');
  }
  const state = newState({ appId: app.appId || null });

  const u = new URL('https://slack.com/oauth/v2/authorize');
  u.searchParams.set('client_id', app.clientId);
  u.searchParams.set('scope', SLACK_BOT_SCOPES.join(','));
  u.searchParams.set('redirect_uri', `${config.publicUrl}/slack/oauth/callback`);
  u.searchParams.set('state', state);
  res.redirect(u.toString());
});

router.get('/slack/oauth/callback', async (req, res) => {
  const { code, state, error } = req.query;
  if (error) return res.status(400).send(`Slack error: ${escapeHtml(error)}`);
  if (!code) return res.status(400).send('missing code');

  const pending = takeState(state);
  if (!pending) return res.status(400).send('state mismatch or expired');

  // Exchange with the same app's client credentials the install was started with.
  const app = config.slack.appById(pending.appId) || config.slack.primary;
  const data = await exchangeCode(code, `${config.publicUrl}/slack/oauth/callback`, app);
  if (!data.ok || !data.access_token) {
    return res.status(400).send(`token exchange failed: ${escapeHtml(data.error || 'unknown')}`);
  }

  const teamId = data.team?.id ?? null;
  const teamName = data.team?.name ?? 'your workspace';
  const botToken = data.access_token;
  // Slack returns the installed app's id; fall back to the profile's configured id.
  const appId = data.app_id || app.appId || null;
  // Name the agent after the Slack app so multiple clones stay distinguishable;
  // for a lone app keep the workspace name (prior behavior).
  const agentName = config.slack.apps.length > 1 ? app.name || teamName : teamName || app.name;

  // Reuse the agent for this (workspace, app) — re-auth — or create a new one.
  let registrationToken;
  let existing = teamId ? await getAgentByTeamAndApp(teamId, appId) : null;
  // Migration: if there's no App-ID-bound agent yet but a legacy agent exists
  // for this workspace (installed before multi-app, so slack_app_id is null),
  // adopt it — stamping its App ID — instead of creating a duplicate.
  if (!existing && teamId && appId) {
    const legacy = await getAgentByTeam(teamId);
    if (legacy && !legacy.slack_app_id) existing = legacy;
  }
  if (existing) {
    await setAgentSlack(existing.id, { teamId, appId, botToken });
    registrationToken = existing.registration_token;
  } else {
    const agent = await createAgent(agentName);
    await setAgentSlack(agent.id, { teamId, appId, botToken });
    registrationToken = agent.registrationToken;
  }

  // Log the installer in and land them on the dashboard (which shows the token
  // + live status for every agent in this workspace).
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

  // Each cloned app has its own signing secret; pick it by the App ID in the
  // payload so all apps verify (falling back to the primary app's secret).
  const apiAppId = req.body?.api_app_id || null;
  const app = config.slack.appById(apiAppId) || config.slack.primary;
  const verified = verifySlackSignature({
    signingSecret: app.signingSecret,
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
    // workspace, AND — when several cloned apps run in one workspace — only to
    // the agent for the app this event came from (matched by App ID). Legacy
    // agents with no slack_app_id match on team alone (single-app behavior).
    // Never broadcast — that would leak one tenant's (or app's) events.
    try {
      const targets = (await listAgents()).filter(
        (a) =>
          a.slack_team_id === teamId &&
          (!a.slack_app_id || !apiAppId || a.slack_app_id === apiAppId),
      );
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
        await notifyOfflineIfAddressed(targets, req.body.event);
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
async function notifyOfflineIfAddressed(targets, event) {
  if (!event || typeof event !== 'object') return;
  if (event.bot_id || _NOTIFY_IGNORED_SUBTYPES.has(event.subtype)) return;

  const channel = event.channel;
  const ts = event.ts;
  if (!channel || !ts) return;
  const threadTs = event.thread_ts || ts;

  // Post with THIS app's bot token (targets are already the agents for this app).
  const agent = await getAgentById(targets[0].id);
  const token = agent && agent.slack_bot_token;
  if (!token) return; // can't post without the app's bot token

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
