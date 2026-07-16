// Slack helpers: signature verification, OAuth code exchange, app manifest.
// Adapted from the old Central, minus the multi-persona / Ably / wake machinery —
// this OSS Central runs a single pre-built Slack app.
import crypto from 'node:crypto';
import { config } from './config.js';

// Bot token scopes requested during OAuth install.
export const SLACK_BOT_SCOPES = [
  'app_mentions:read',
  'chat:write',
  'channels:history',
  'groups:history',
  'im:history',
  'mpim:history',
  'im:read',
  'im:write',
  'users:read',
  'files:write',
  'files:read',
];

const MAX_SKEW_S = 60 * 5; // reject requests older than 5 min (replay protection)

// Verify an inbound Slack request signature. The signed base string is
// `v0:<ts>:<raw body>`, so the caller MUST pass the exact raw request body.
export function verifySlackSignature({ signingSecret, signature, timestamp, rawBody }) {
  if (!signingSecret) return true; // dev mode: no secret configured
  if (!signature || !timestamp) return false;
  const ts = Number(timestamp);
  if (!Number.isFinite(ts)) return false;
  if (Math.abs(Date.now() / 1000 - ts) > MAX_SKEW_S) return false;

  const base = `v0:${timestamp}:${rawBody}`;
  const digest = crypto.createHmac('sha256', signingSecret).update(base).digest('hex');
  const expected = `v0=${digest}`;
  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

// --- Sign in with Slack (OIDC) ----------------------------------------------
// User-level identity (not a bot install), used to authenticate a workspace
// member for the dashboard. Same app credentials, different endpoints/scopes.

export function oidcAuthorizeUrl(state, redirectUri) {
  const u = new URL('https://slack.com/openid/connect/authorize');
  u.searchParams.set('response_type', 'code');
  u.searchParams.set('scope', 'openid email profile');
  u.searchParams.set('client_id', config.slack.clientId);
  u.searchParams.set('redirect_uri', redirectUri);
  u.searchParams.set('state', state);
  return u.toString();
}

export async function exchangeOidcCode(code, redirectUri) {
  const res = await fetch('https://slack.com/api/openid.connect.token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: config.slack.clientId,
      client_secret: config.slack.clientSecret,
      code,
      redirect_uri: redirectUri,
    }),
  });
  return res.json();
}

// Decode the id_token JWT claims. The token is delivered server-to-server from
// Slack's token endpoint over TLS in response to our client-secret-authenticated
// request, so it's trusted without JWKS verification (optional hardening).
export function decodeIdToken(idToken) {
  try {
    const [, payload] = idToken.split('.');
    const claims = JSON.parse(Buffer.from(payload, 'base64url').toString());
    return {
      teamId: claims['https://slack.com/team_id'] || null,
      teamName: claims['https://slack.com/team_name'] || null,
      userId: claims['https://slack.com/user_id'] || claims.sub || null,
      name: claims.name || null,
      email: claims.email || null,
    };
  } catch {
    return null;
  }
}

// Exchange an OAuth `code` for a bot token (oauth.v2.access).
export async function exchangeCode(code, redirectUri) {
  const res = await fetch('https://slack.com/api/oauth.v2.access', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: config.slack.clientId,
      client_secret: config.slack.clientSecret,
      code,
      redirect_uri: redirectUri,
    }),
  });
  return res.json();
}

// The Slack app manifest for this instance. Paste into "Create New App → From a
// manifest" once, then fill SLACK_CLIENT_ID / SLACK_CLIENT_SECRET / signing
// secret into Central's env. Socket Mode OFF — events arrive over the webhook.
export function buildManifest({ appName, baseUrl }) {
  return {
    display_information: { name: appName },
    features: {
      bot_user: { display_name: appName, always_online: true },
    },
    oauth_config: {
      redirect_urls: [`${baseUrl}/slack/oauth/callback`, `${baseUrl}/auth/slack/callback`],
      scopes: {
        bot: SLACK_BOT_SCOPES,
        // Sign in with Slack (dashboard auth).
        user: ['openid', 'email', 'profile'],
      },
    },
    settings: {
      event_subscriptions: {
        request_url: `${baseUrl}/slack/events`,
        // message.channels/groups/mpim let the bot follow up on plain thread
        // replies (no re-@mention needed); the bridge only acts on replies in
        // threads it's already engaged in. Scopes for these are in
        // SLACK_BOT_SCOPES (channels:history / groups:history / mpim:history).
        bot_events: [
          'app_mention',
          'message.im',
          'message.channels',
          'message.groups',
          'message.mpim',
        ],
      },
      interactivity: { is_enabled: false },
      org_deploy_enabled: false,
      socket_mode_enabled: false,
      token_rotation_enabled: false,
    },
  };
}
