// WebSocket hub: one connection per agent. On connect we replay any events the
// agent missed (seq > lastSeq), then live-push new ones. The agent acks by seq.
import { WebSocketServer } from 'ws';
import { MSG } from './protocol.js';
import { getAgentByToken, eventsAfter, ackSeq, touchAgentSeen } from './store.js';

const connections = new Map(); // agentId -> { ws, ready, queue }

// Agent ids with a live WebSocket right now (in-memory; single instance).
export function onlineIds() {
  const ids = new Set();
  for (const [id, conn] of connections) {
    if (conn.ws.readyState === conn.ws.OPEN) ids.add(id);
  }
  return ids;
}

// Threads we've already told "the agent is offline" during the current offline
// episode, so a user firing several messages at a down agent-wrapper gets one notice,
// not one per message. Reset for an agent when it reconnects.
const offlineNotified = new Map(); // agentId -> Set(threadKey)

// Returns true if the caller should post an offline notice for this thread
// (first time this episode), false if we've already notified.
export function claimOfflineNotice(agentId, threadKey) {
  let set = offlineNotified.get(agentId);
  if (!set) {
    set = new Set();
    offlineNotified.set(agentId, set);
  }
  if (set.has(threadKey)) return false;
  set.add(threadKey);
  return true;
}

function send(ws, ev) {
  if (ws.readyState !== ws.OPEN) return;
  ws.send(
    JSON.stringify({
      type: MSG.EVENT,
      seq: ev.seq,
      event: { type: ev.type, payload: ev.payload },
    }),
  );
}

export function attachWsHub(server) {
  const wss = new WebSocketServer({ server, path: '/ws' });

  wss.on('connection', async (ws, req) => {
    const url = new URL(req.url, 'http://localhost');
    const token = url.searchParams.get('token');
    const clientLastSeq = Number(url.searchParams.get('lastSeq') || 0);

    let agent = null;
    try {
      agent = token ? await getAgentByToken(token) : null;
    } catch (e) {
      console.error('[ws] auth lookup failed:', e);
    }
    if (!agent) {
      ws.close(4001, 'invalid registration token');
      return;
    }

    // Replay from the furthest-forward cursor we know: the client's last-seen
    // seq OR the server-recorded ack high-water mark, whichever is higher. This
    // prevents a reinstalled agent (fresh local state → lastSeq=0) from replaying
    // — and re-processing — events it already acked. Acked events are also pruned
    // (see ackSeq), so a caught-up agent has nothing to replay.
    const lastSeq = Math.max(clientLastSeq, Number(agent.last_acked_seq || 0));

    // Register the connection immediately. Live events that arrive during the
    // (async) replay below are buffered on `queue` and flushed afterwards, so
    // nothing is dropped in the window between reading the log and going live.
    const conn = { ws, ready: false, queue: [] };
    connections.set(agent.id, conn);
    offlineNotified.delete(agent.id); // fresh episode: allow notices again
    touchAgentSeen(agent.id).catch(() => {});
    console.log(`[ws] agent ${agent.id} connected (lastSeq=${lastSeq})`);

    ws.on('message', (data) => {
      let msg;
      try {
        msg = JSON.parse(data.toString());
      } catch {
        return;
      }
      if (msg.type === MSG.ACK && typeof msg.seq === 'number') {
        ackSeq(agent.id, msg.seq).catch((e) => console.warn('[ws] ack failed:', e));
      }
    });

    ws.on('close', () => {
      if (connections.get(agent.id) === conn) connections.delete(agent.id);
      touchAgentSeen(agent.id).catch(() => {});
      console.log(`[ws] agent ${agent.id} disconnected`);
    });

    try {
      const missed = await eventsAfter(agent.id, lastSeq);
      for (const ev of missed) send(ws, ev);
      // Flush live events queued during replay, skipping any already replayed.
      const replayedMax = missed.length ? missed[missed.length - 1].seq : lastSeq;
      for (const ev of conn.queue) if (ev.seq > replayedMax) send(ws, ev);
    } catch (e) {
      console.error('[ws] replay failed:', e);
    } finally {
      conn.queue = [];
      conn.ready = true;
    }
  });
}

// Live-push an appended event to the agent if it is currently connected. During
// the initial replay it's queued; if the agent is offline it stays in the log
// and is replayed on the next connect.
export function pushEvent(agentId, ev) {
  const conn = connections.get(agentId);
  if (!conn) return;
  if (!conn.ready) {
    conn.queue.push(ev);
    return;
  }
  send(conn.ws, ev);
}
