import crypto from 'node:crypto';

const truthy = (v) => ['1', 'true', 'yes', 'on'].includes((v || '').toLowerCase());
const publicUrl = process.env.PUBLIC_URL || 'http://localhost:3000';

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
    signingSecret: process.env.SLACK_SIGNING_SECRET || '',
    botToken: process.env.SLACK_BOT_TOKEN || '', // optional fallback bot token
    clientId: process.env.SLACK_CLIENT_ID || '',
    clientSecret: process.env.SLACK_CLIENT_SECRET || '',
    appName: process.env.SLACK_APP_NAME || 'Claudebot',
  },
};

if (!process.env.SESSION_SECRET) {
  console.warn('[central-dispatch] SESSION_SECRET not set — dashboard sessions reset on restart');
}
