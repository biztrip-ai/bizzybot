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
    const lastSeq = Number(url.searchParams.get('lastSeq') || 0);

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

    // Register the connection immediately. Live events that arrive during the
    // (async) replay below are buffered on `queue` and flushed afterwards, so
    // nothing is dropped in the window between reading the log and going live.
    const conn = { ws, ready: false, queue: [] };
    connections.set(agent.id, conn);
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
