// Plain data-access functions over the db layer. Async so the same code works on
// SQLite (sync driver, resolves immediately) and Postgres (async driver).
import { randomUUID, randomBytes } from 'node:crypto';
import { get, all, run, tx, isPg, AGENTS, EVENTS } from './db.js';

export async function createAgent(name) {
  const id = randomUUID();
  const registrationToken = randomBytes(24).toString('hex');
  await run(
    `INSERT INTO ${AGENTS} (id, name, registration_token, created_at) VALUES (?, ?, ?, ?)`,
    [id, name, registrationToken, Date.now()],
  );
  return { id, name, registrationToken };
}

export async function getAgentByToken(token) {
  return get(`SELECT * FROM ${AGENTS} WHERE registration_token = ?`, [token]);
}

export async function listAgents() {
  const rows = await all(
    `SELECT id, name, slack_team_id, registered_at, last_seen_at,
            CASE WHEN slack_bot_token IS NOT NULL AND slack_bot_token <> '' THEN 1 ELSE 0 END AS slack_connected
       FROM ${AGENTS} ORDER BY created_at`,
  );
  return rows.map((r) => ({
    ...r,
    slack_connected: Boolean(Number(r.slack_connected)),
  }));
}

// Record that we just saw the agent's agent-wrapper (connect/disconnect), for the
// "last seen" indicator when it's offline.
export async function touchAgentSeen(id) {
  await run(`UPDATE ${AGENTS} SET last_seen_at = ? WHERE id = ?`, [Date.now(), id]);
}

// The (first) agent bound to a Slack workspace, so re-authorizing the same
// workspace reuses its agent + registration token instead of piling up new ones.
export async function getAgentByTeam(teamId) {
  return get(
    `SELECT * FROM ${AGENTS} WHERE slack_team_id = ? ORDER BY created_at LIMIT 1`,
    [teamId],
  );
}

export async function setAgentSlack(id, { teamId, botToken } = {}) {
  await run(`UPDATE ${AGENTS} SET slack_team_id = ?, slack_bot_token = ? WHERE id = ?`, [
    teamId ?? null,
    botToken ?? null,
    id,
  ]);
}

export async function markRegistered(id, { teamId, botToken } = {}) {
  await run(
    `UPDATE ${AGENTS}
        SET registered_at   = ?,
            slack_team_id    = COALESCE(?, slack_team_id),
            slack_bot_token  = COALESCE(?, slack_bot_token)
      WHERE id = ?`,
    [Date.now(), teamId ?? null, botToken ?? null, id],
  );
}

// Append an event with the next per-agent sequence number. The seq comes from an
// atomic bump of the agent's `event_seq` counter, so concurrent appends to the
// same agent get distinct, gap-free, monotonically increasing seqs (no lost
// events). On Postgres this runs in a transaction (the counter bump takes a row
// lock, serializing per agent, and a failed insert rolls the bump back). SQLite
// is single-process/synchronous, so the two statements suffice. Seq order equals
// insert order, which the replay logic in wsHub relies on.
export async function appendEvent(agentId, type, payload) {
  const data = JSON.stringify(payload);
  const doIt = async (q) => {
    const row = await q.get(
      `UPDATE ${AGENTS} SET event_seq = event_seq + 1 WHERE id = ? RETURNING event_seq AS seq`,
      [agentId],
    );
    const seq = Number(row.seq);
    await q.run(
      `INSERT INTO ${EVENTS} (agent_id, seq, type, payload, created_at)
       VALUES (?, ?, ?, ?, ?)`,
      [agentId, seq, type, data, Date.now()],
    );
    return { seq, type, payload };
  };
  return isPg ? tx(doIt) : doIt({ get, run });
}

export async function eventsAfter(agentId, afterSeq) {
  const rows = await all(
    `SELECT seq, type, payload FROM ${EVENTS}
      WHERE agent_id = ? AND seq > ? ORDER BY seq ASC`,
    [agentId, afterSeq],
  );
  return rows.map((r) => ({ seq: Number(r.seq), type: r.type, payload: JSON.parse(r.payload) }));
}

export async function ackSeq(agentId, seq) {
  await run(`UPDATE ${AGENTS} SET last_acked_seq = ? WHERE id = ? AND ? > last_acked_seq`, [
    seq,
    agentId,
    seq,
  ]);
  // An ack means the agent processed everything up to `seq`, so remove those
  // events from the log — it's a queue, not an archive. Keeps storage bounded
  // and means a reconnect never re-delivers already-processed events.
  await run(`DELETE FROM ${EVENTS} WHERE agent_id = ? AND seq <= ?`, [agentId, seq]);
}
