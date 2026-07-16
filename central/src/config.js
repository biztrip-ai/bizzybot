const truthy = (v) => ['1', 'true', 'yes', 'on'].includes((v || '').toLowerCase());

// All configuration comes from environment variables. Nothing fancy.
export const config = {
  port: Number(process.env.PORT || 3000),
  publicUrl: process.env.PUBLIC_URL || 'http://localhost:3000',
  // Storage: if DATABASE_URL is set, use Postgres (e.g. Neon) with a dedicated
  // schema so it can share a database with other apps; otherwise fall back to a
  // local SQLite file.
  databaseUrl: process.env.DATABASE_URL || '',
  dbSchema: process.env.DB_SCHEMA || 'claudebot',
  dbPath: process.env.DB_PATH || './central.db',
  adminKey: process.env.ADMIN_KEY || '',
  // Serve TLS locally with a self-signed cert so Slack's OAuth redirect to
  // https://localhost works. Leave OFF on Railway (the platform terminates TLS
  // and the app should listen plain HTTP behind it).
  tlsSelfSigned: truthy(process.env.TLS_SELF_SIGNED),
  certDir: process.env.CERT_DIR || './.certs',
  slack: {
    signingSecret: process.env.SLACK_SIGNING_SECRET || '',
    botToken: process.env.SLACK_BOT_TOKEN || '', // fallback for the admin path
    clientId: process.env.SLACK_CLIENT_ID || '',
    clientSecret: process.env.SLACK_CLIENT_SECRET || '',
    appName: process.env.SLACK_APP_NAME || 'Claudebot',
  },
};
