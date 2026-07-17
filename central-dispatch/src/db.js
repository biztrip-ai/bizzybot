// Dual storage backend. If DATABASE_URL is set, use Postgres (e.g. Neon) with a
// dedicated schema; otherwise use the built-in node:sqlite. One small async
// interface (get/all/run) over both — store.js writes portable SQL with `?`
// placeholders, which we translate to $1..$n for Postgres.
import { config } from './config.js';

const usePg = Boolean(config.databaseUrl);

// Table identifiers. Postgres tables live in a schema so they don't collide with
// other apps sharing the database; SQLite uses plain names.
export const AGENTS = usePg ? `${config.dbSchema}.agents` : 'agents';
export const EVENTS = usePg ? `${config.dbSchema}.events` : 'events';
export const WORKSPACE_SETTINGS = usePg
  ? `${config.dbSchema}.workspace_settings`
  : 'workspace_settings';

let pool = null;
let sqlite = null;

if (usePg) {
  const pg = (await import('pg')).default;
  // int8/bigint -> JS number. Our ids/seqs/timestamps are all well under 2^53.
  pg.types.setTypeParser(20, (v) => parseInt(v, 10));
  const url = config.databaseUrl;
  const ssl = /neon\.tech|sslmode=require/.test(url) ? { rejectUnauthorized: false } : undefined;
  pool = new pg.Pool({ connectionString: url, ssl });
} else {
  const { DatabaseSync } = await import('node:sqlite');
  sqlite = new DatabaseSync(config.dbPath);
}

function toPg(sql) {
  let i = 0;
  return sql.replace(/\?/g, () => `$${++i}`);
}

export async function run(sql, params = []) {
  if (usePg) {
    await pool.query(toPg(sql), params);
    return;
  }
  sqlite.prepare(sql).run(...params);
}

export async function get(sql, params = []) {
  if (usePg) return (await pool.query(toPg(sql), params)).rows[0];
  return sqlite.prepare(sql).get(...params);
}

export async function all(sql, params = []) {
  if (usePg) return (await pool.query(toPg(sql), params)).rows;
  return sqlite.prepare(sql).all(...params);
}

export const isPg = usePg;

// Run `fn` inside a Postgres transaction on a dedicated pooled client. Used by
// appendEvent so the per-agent sequence bump + event insert are atomic and
// serialized per agent (a row lock on the agent). SQLite (single-process, sync)
// doesn't need this — callers pass the module-level get/run directly.
export async function tx(fn) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const q = {
      get: async (sql, p = []) => (await client.query(toPg(sql), p)).rows[0],
      run: async (sql, p = []) => {
        await client.query(toPg(sql), p);
      },
    };
    const result = await fn(q);
    await client.query('COMMIT');
    return result;
  } catch (e) {
    await client.query('ROLLBACK');
    throw e;
  } finally {
    client.release();
  }
}

export async function init() {
  if (usePg) {
    await pool.query(`CREATE SCHEMA IF NOT EXISTS ${config.dbSchema}`);
    await pool.query(`
      CREATE TABLE IF NOT EXISTS ${AGENTS} (
        id                 TEXT PRIMARY KEY,
        name               TEXT NOT NULL,
        registration_token TEXT NOT NULL UNIQUE,
        slack_team_id      TEXT,
        slack_bot_token    TEXT,
        created_at         BIGINT NOT NULL,
        registered_at      BIGINT,
        last_acked_seq     BIGINT NOT NULL DEFAULT 0,
        event_seq          BIGINT NOT NULL DEFAULT 0,
        last_seen_at       BIGINT
      )`);
    // Idempotent migrations for tables that may predate a column.
    await pool.query(`ALTER TABLE ${AGENTS} ADD COLUMN IF NOT EXISTS last_seen_at BIGINT`);
    await pool.query(`ALTER TABLE ${AGENTS} ADD COLUMN IF NOT EXISTS slack_app_id TEXT`);
    // Inbound-email columns (see docs/EMAIL.md).
    await pool.query(`ALTER TABLE ${AGENTS} ADD COLUMN IF NOT EXISTS email_local_part TEXT`);
    await pool.query(`ALTER TABLE ${AGENTS} ADD COLUMN IF NOT EXISTS email_channel TEXT`);
    await pool.query(`ALTER TABLE ${AGENTS} ADD COLUMN IF NOT EXISTS email_sender_allow TEXT`);
    // A local-part must be unique within a workspace (same local-part on two
    // different workspace domains is fine). Partial: only enforced when set.
    await pool.query(
      `CREATE UNIQUE INDEX IF NOT EXISTS agents_team_email_local_part
         ON ${AGENTS} (slack_team_id, email_local_part)
       WHERE email_local_part IS NOT NULL`,
    );
    await pool.query(`
      CREATE TABLE IF NOT EXISTS ${EVENTS} (
        id         BIGSERIAL PRIMARY KEY,
        agent_id   TEXT NOT NULL,
        seq        BIGINT NOT NULL,
        type       TEXT NOT NULL,
        payload    TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        UNIQUE (agent_id, seq)
      )`);
    // Per-workspace Mailgun config (domain + account-wide key) + the workspace's
    // default sender allow-list, set on the dashboard.
    await pool.query(`
      CREATE TABLE IF NOT EXISTS ${WORKSPACE_SETTINGS} (
        team_id          TEXT PRIMARY KEY,
        mailgun_domain   TEXT,
        mailgun_api_key  TEXT,
        mailgun_base_url TEXT,
        sender_allow     TEXT,
        updated_at       BIGINT NOT NULL
      )`);
    await pool.query(`ALTER TABLE ${WORKSPACE_SETTINGS} ADD COLUMN IF NOT EXISTS sender_allow TEXT`);
    console.log(`[central-dispatch] storage: Postgres (schema "${config.dbSchema}")`);
  } else {
    sqlite.exec(`
      CREATE TABLE IF NOT EXISTS agents (
        id                 TEXT PRIMARY KEY,
        name               TEXT NOT NULL,
        registration_token TEXT NOT NULL UNIQUE,
        slack_team_id      TEXT,
        slack_app_id       TEXT,
        slack_bot_token    TEXT,
        created_at         INTEGER NOT NULL,
        registered_at      INTEGER,
        last_acked_seq     INTEGER NOT NULL DEFAULT 0,
        event_seq          INTEGER NOT NULL DEFAULT 0,
        last_seen_at       INTEGER
      );
      CREATE TABLE IF NOT EXISTS events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id   TEXT NOT NULL,
        seq        INTEGER NOT NULL,
        type       TEXT NOT NULL,
        payload    TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE (agent_id, seq)
      );
      CREATE TABLE IF NOT EXISTS workspace_settings (
        team_id          TEXT PRIMARY KEY,
        mailgun_domain   TEXT,
        mailgun_api_key  TEXT,
        mailgun_base_url TEXT,
        sender_allow     TEXT,
        updated_at       INTEGER NOT NULL
      );`);
    // Idempotent migration for older DBs (SQLite has no ADD COLUMN IF NOT EXISTS).
    const cols = sqlite.prepare(`PRAGMA table_info(agents)`).all().map((c) => c.name);
    if (!cols.includes('last_seen_at')) {
      sqlite.exec(`ALTER TABLE agents ADD COLUMN last_seen_at INTEGER`);
    }
    if (!cols.includes('slack_app_id')) {
      sqlite.exec(`ALTER TABLE agents ADD COLUMN slack_app_id TEXT`);
    }
    // Inbound-email columns (see docs/EMAIL.md).
    if (!cols.includes('email_local_part')) {
      sqlite.exec(`ALTER TABLE agents ADD COLUMN email_local_part TEXT`);
    }
    if (!cols.includes('email_channel')) {
      sqlite.exec(`ALTER TABLE agents ADD COLUMN email_channel TEXT`);
    }
    if (!cols.includes('email_sender_allow')) {
      sqlite.exec(`ALTER TABLE agents ADD COLUMN email_sender_allow TEXT`);
    }
    sqlite.exec(
      `CREATE UNIQUE INDEX IF NOT EXISTS agents_team_email_local_part
         ON agents (slack_team_id, email_local_part)
       WHERE email_local_part IS NOT NULL`,
    );
    const wsCols = sqlite.prepare(`PRAGMA table_info(workspace_settings)`).all().map((c) => c.name);
    if (!wsCols.includes('sender_allow')) {
      sqlite.exec(`ALTER TABLE workspace_settings ADD COLUMN sender_allow TEXT`);
    }
    console.log(`[central-dispatch] storage: SQLite (${config.dbPath})`);
  }
}
