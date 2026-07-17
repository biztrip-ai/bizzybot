import crypto from 'node:crypto';

const truthy = (v) => ['1', 'true', 'yes', 'on'].includes((v || '').toLowerCase());
const publicUrl = process.env.PUBLIC_URL || 'http://localhost:3000';

// One or more Slack apps back this deployment. Cloning the Slack app (same
// manifest, different name) lets several agents run in one workspace — they all
// share the workspace's team_id and differ only by App ID / bot token / creds.
// Configure them as a JSON array in SLACK_APPS; if unset we synthesize a single
// profile from the legacy SLACK_* vars so existing single-app deploys keep working.
function parseSlackApps() {
  const raw = process.env.SLACK_APPS;
  if (raw && raw.trim()) {
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length) {
        return arr.map((a) => ({
          appId: a.appId || a.app_id || '',
          name: a.name || 'Bizzybot',
          clientId: a.clientId || a.client_id || '',
          clientSecret: a.clientSecret || a.client_secret || '',
          signingSecret: a.signingSecret || a.signing_secret || '',
        }));
      }
      console.error('[central-dispatch] SLACK_APPS must be a non-empty JSON array; ignoring.');
    } catch (e) {
      console.error('[central-dispatch] SLACK_APPS is not valid JSON, ignoring:', e.message);
    }
  }
  return [
    {
      appId: process.env.SLACK_APP_ID || '',
      name: process.env.SLACK_APP_NAME || 'Bizzybot',
      clientId: process.env.SLACK_CLIENT_ID || '',
      clientSecret: process.env.SLACK_CLIENT_SECRET || '',
      signingSecret: process.env.SLACK_SIGNING_SECRET || '',
    },
  ];
}

const slackApps = parseSlackApps();
const primaryApp = slackApps[0];

// All configuration comes from environment variables. Nothing fancy.
export const config = {
  port: Number(process.env.PORT || 3000),
  publicUrl,
  // Signs the "Sign in with Slack" session cookie. Set SESSION_SECRET in prod so
  // sessions survive restarts; a random dev value is used otherwise.
  sessionSecret: process.env.SESSION_SECRET || crypto.randomBytes(32).toString('hex'),
  cookieSecure: publicUrl.startsWith('https'),
  // Storage: if DATABASE_URL is set, use Postgres (e.g. Neon) with a dedicated
  // schema so it can share a database with other apps; otherwise fall back to a
  // local SQLite file.
  databaseUrl: process.env.DATABASE_URL || '',
  dbSchema: process.env.DB_SCHEMA || 'claudebot',
  dbPath: process.env.DB_PATH || './central-dispatch.db',
  // Serve TLS locally with a self-signed cert so Slack's OAuth redirect to
  // https://localhost works. Leave OFF on Railway (the platform terminates TLS
  // and the app should listen plain HTTP behind it).
  tlsSelfSigned: truthy(process.env.TLS_SELF_SIGNED),
  certDir: process.env.CERT_DIR || './.certs',
  slack: {
    // The configured app profiles, plus a helper to resolve one by Slack App ID.
    apps: slackApps,
    primary: primaryApp,
    appById: (id) => (id ? slackApps.find((a) => a.appId && a.appId === id) : null) || null,
    botToken: process.env.SLACK_BOT_TOKEN || '', // optional fallback bot token
    // Back-compat aliases pointing at the primary app (used for sign-in / the
    // "is Slack configured?" check).
    signingSecret: primaryApp.signingSecret,
    clientId: primaryApp.clientId,
    clientSecret: primaryApp.clientSecret,
    appName: primaryApp.name,
  },
};

if (!process.env.SESSION_SECRET) {
  console.warn('[central-dispatch] SESSION_SECRET not set — dashboard sessions reset on restart');
}
