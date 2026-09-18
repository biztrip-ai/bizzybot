// Secret dropbox: move secrets (tokens, env var values, keys) from a human to an
// agent box without them passing through chat, logs, or this server in the clear.
//
//   1. The box runs `bizzybot-dropbox request …`: it makes a one-time RSA key
//      pair and POSTs the public key + the item names it needs + a note here.
//      It gets back a URL (unguessable id) and a pickup secret.
//   2. The human opens the URL. The page encrypts the values *in the browser*
//      to the box's public key (RSA-OAEP-wrapped AES-GCM, AAD = dropbox id) and
//      POSTs only ciphertext. A check code derived from the public key is shown
//      on the page and printed by the box, so the human can confirm they're
//      filling in the right request.
//   3. The box polls with its pickup secret, receives the ciphertext, decrypts
//      locally, and the row is deleted.
//
// No sign-in by design (one-time use): anyone with the URL can *submit*, but
// never read. Mitigations: 128-bit ids, short expiry, a single submission, and
// the box accepts only the item names it asked for.
import crypto from 'node:crypto';
import express from 'express';
import { config } from './config.js';
import { get, run, isPg } from './db.js';

const DROPBOXES = isPg ? `${config.dbSchema}.dropboxes` : 'dropboxes';

const DEFAULT_TTL_MIN = 30;
const MAX_TTL_MIN = 120;
const MAX_ITEMS = 20;
const MAX_NOTE = 4000;
const MAX_CIPHERTEXT = 90 * 1024; // base64 chars; under express.json's 100kb body limit
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/;

export async function initDropboxTable() {
  const big = isPg ? 'BIGINT' : 'INTEGER';
  await run(`
    CREATE TABLE IF NOT EXISTS ${DROPBOXES} (
      id              TEXT PRIMARY KEY,
      public_key      TEXT NOT NULL,
      items           TEXT NOT NULL,
      note            TEXT,
      pickup_hash     TEXT NOT NULL,
      created_at      ${big} NOT NULL,
      expires_at      ${big} NOT NULL,
      submitted_at    ${big},
      ciphertext      TEXT
    )`);
}

const sha256 = (s) => crypto.createHash('sha256').update(s).digest('hex');
const sameHash = (a, b) =>
  a.length === b.length && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
// Express 4 doesn't catch async rejections; route them to the error handler.
const h = (fn) => (req, res, next) => fn(req, res).catch(next);
const now = () => Date.now();

// Hard-delete anything expired for over an hour (submitted or not). Called
// opportunistically from the handlers; no timer needed.
async function sweep() {
  await run(`DELETE FROM ${DROPBOXES} WHERE expires_at < ?`, [now() - 60 * 60 * 1000]);
}

// Tiny in-memory rate limit for creation (single-process Central-Dispatch).
const createHits = new Map(); // ip -> [timestamps]
function rateLimited(ip, limit = 30, windowMs = 60 * 60 * 1000) {
  const t = now();
  const hits = (createHits.get(ip) || []).filter((x) => t - x < windowMs);
  hits.push(t);
  createHits.set(ip, hits);
  return hits.length > limit;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function validItems(items) {
  if (!Array.isArray(items) || items.length === 0 || items.length > MAX_ITEMS) return null;
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const name = String(it?.name || '');
    if (!NAME_RE.test(name) || seen.has(name)) return null;
    seen.add(name);
    out.push({
      name,
      multiline: Boolean(it.multiline),
      description: String(it.description || '').slice(0, 500),
    });
  }
  return out;
}

export const dropboxRouter = express.Router();

// Box → create a dropbox.
dropboxRouter.post('/api/dropbox', h(async (req, res) => {
  // Behind Railway's proxy req.ip is the proxy; use the first forwarded hop.
  const ip = String(req.get('x-forwarded-for') || req.ip || '').split(',')[0].trim();
  if (rateLimited(ip)) return res.status(429).json({ error: 'rate_limited' });
  const publicKey = String(req.body?.public_key || '');
  const items = validItems(req.body?.items);
  if (!/^[A-Za-z0-9+/=]{200,2000}$/.test(publicKey) || !items) {
    return res.status(400).json({ error: 'invalid_request' });
  }
  const ttlMin = Math.min(Math.max(Number(req.body?.ttl_minutes) || DEFAULT_TTL_MIN, 1), MAX_TTL_MIN);
  const note = String(req.body?.note || '').slice(0, MAX_NOTE);
  const id = crypto.randomBytes(16).toString('base64url');
  const pickup = crypto.randomBytes(32).toString('base64url');
  const t = now();
  await sweep();
  await run(
    `INSERT INTO ${DROPBOXES} (id, public_key, items, note, pickup_hash, created_at, expires_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [id, publicKey, JSON.stringify(items), note, sha256(pickup), t, t + ttlMin * 60 * 1000],
  );
  res.json({
    id,
    url: `${config.publicUrl}/drop/${id}`,
    pickup_secret: pickup,
    expires_at: t + ttlMin * 60 * 1000,
  });
}));

// Human's browser → submit ciphertext (once).
dropboxRouter.post('/api/dropbox/:id/submit', h(async (req, res) => {
  const { wrapped_key: wk, iv, ciphertext: ct } = req.body || {};
  const b64 = /^[A-Za-z0-9+/=]+$/;
  if (![wk, iv, ct].every((v) => typeof v === 'string' && b64.test(v)) || ct.length > MAX_CIPHERTEXT) {
    return res.status(400).json({ error: 'invalid_submission' });
  }
  const row = await get(`SELECT expires_at, submitted_at FROM ${DROPBOXES} WHERE id = ?`, [req.params.id]);
  if (!row) return res.status(404).json({ error: 'not_found' });
  if (row.submitted_at) return res.status(409).json({ error: 'already_submitted' });
  if (Number(row.expires_at) < now()) return res.status(410).json({ error: 'expired' });
  // Conditional update so two racing submits can't both win.
  await run(
    `UPDATE ${DROPBOXES} SET ciphertext = ?, submitted_at = ?
      WHERE id = ? AND submitted_at IS NULL AND expires_at >= ?`,
    [JSON.stringify({ wrapped_key: wk, iv, ciphertext: ct }), now(), req.params.id, now()],
  );
  const after = await get(`SELECT ciphertext FROM ${DROPBOXES} WHERE id = ?`, [req.params.id]);
  if (!after?.ciphertext) return res.status(409).json({ error: 'already_submitted' });
  res.json({ ok: true });
}));

// Box → poll for the result with its pickup secret. 204 = not yet.
dropboxRouter.get('/api/dropbox/:id/result', h(async (req, res) => {
  const auth = String(req.get('authorization') || '').replace(/^Bearer\s+/i, '');
  const row = await get(`SELECT * FROM ${DROPBOXES} WHERE id = ?`, [req.params.id]);
  if (!row || !auth || !sameHash(sha256(auth), row.pickup_hash)) {
    return res.status(404).json({ error: 'not_found' });
  }
  if (!row.ciphertext) {
    if (Number(row.expires_at) < now()) {
      await run(`DELETE FROM ${DROPBOXES} WHERE id = ?`, [row.id]);
      return res.status(410).json({ error: 'expired' });
    }
    return res.status(204).end();
  }
  // Hand over once, then forget it.
  await run(`DELETE FROM ${DROPBOXES} WHERE id = ?`, [row.id]);
  res.json(JSON.parse(row.ciphertext));
}));

// Human → the page.
dropboxRouter.get('/drop/:id', h(async (req, res) => {
  res.set({
    'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
    'X-Robots-Tag': 'noindex',
  });
  const row = await get(`SELECT * FROM ${DROPBOXES} WHERE id = ?`, [req.params.id]);
  const shell = (body, extraHead = '') => `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secret dropbox</title>${extraHead}
<body style="font-family:system-ui;max-width:640px;margin:48px auto;padding:0 16px;line-height:1.45">
<h1 style="margin-bottom:4px">🔐 Secret dropbox</h1>${body}</body>`;
  if (!row || Number(row.expires_at) < now()) {
    return res.status(404).type('html').send(shell('<p>This dropbox doesn’t exist or has expired. Ask for a new link.</p>'));
  }
  if (row.submitted_at) {
    return res.status(410).type('html').send(shell('<p>✅ Already submitted. This link can’t be used again.</p>'));
  }
  const items = JSON.parse(row.items);
  const nonce = crypto.randomBytes(16).toString('base64');
  res.set(
    'Content-Security-Policy',
    `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
  );
  const mins = Math.max(1, Math.round((Number(row.expires_at) - now()) / 60000));
  const fields = items
    .map((it, i) => {
      const input = it.multiline
        ? `<textarea id="f${i}" rows="6" autocomplete="off" spellcheck="false" style="width:100%;font-family:ui-monospace,monospace;font-size:13px"></textarea>`
        : `<input id="f${i}" type="password" autocomplete="off" spellcheck="false" style="width:100%;font-family:ui-monospace,monospace;font-size:14px;padding:6px">`;
      return `<div style="margin:14px 0">
  <label for="f${i}"><code style="font-weight:600">${escapeHtml(it.name)}</code></label>
  ${it.description ? `<div style="color:#555;font-size:14px;margin:2px 0 4px">${escapeHtml(it.description)}</div>` : ''}
  ${input}
</div>`;
    })
    .join('');
  res.type('html').send(
    shell(`
<p style="color:#555;margin-top:0">Values are encrypted in this browser to the requesting machine’s key.
This server only ever sees ciphertext. One use; expires in about ${mins} min.</p>
${row.note ? `<div style="white-space:pre-wrap;background:#f6f6f6;border-radius:6px;padding:12px;margin:16px 0">${escapeHtml(row.note)}</div>` : ''}
<p>Check code: <b id="code" style="font-family:ui-monospace,monospace;font-size:18px">…</b>
<span style="color:#555;font-size:14px">— it should match the code the requester gave you.</span></p>
<form id="form">${fields}
<button id="go" type="submit" style="background:#4A154B;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-size:15px;cursor:pointer">Encrypt &amp; send</button>
<span id="status" style="margin-left:10px"></span>
</form>
<script nonce="${nonce}">
${browserScript}
start(${JSON.stringify({ id: row.id, publicKey: row.public_key, items: items.map((it) => ({ name: it.name, multiline: it.multiline })) })});
</script>`),
  );
}));

// Runs in the browser. Kept as a string so the page stays a single response.
// Mirrored by the Python client (bizzybot_agent_wrapper/dropbox.py): RSA-OAEP
// (SHA-256) wraps a random AES-256-GCM key; the GCM AAD is the dropbox id.
const browserScript = String.raw`
const b64 = (buf) => {
  const u = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s);
};
const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
async function checkCode(spkiB64) {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', unb64(spkiB64)));
  const A = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  let s = '';
  for (let i = 0; i < 6; i++) s += A[d[i] % A.length];
  return s.slice(0, 3) + '-' + s.slice(3);
}
async function start(cfg) {
  document.getElementById('code').textContent = await checkCode(cfg.publicKey);
  const rsa = await crypto.subtle.importKey('spki', unb64(cfg.publicKey),
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
  const form = document.getElementById('form');
  const status = document.getElementById('status');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const values = {};
    for (let i = 0; i < cfg.items.length; i++) {
      const it = cfg.items[i];
      let v = document.getElementById('f' + i).value;
      if (!it.multiline) v = v.trim();
      if (v === '') { status.textContent = 'Fill in ' + it.name; return; }
      if (!it.multiline && /[\r\n]/.test(v)) { status.textContent = it.name + ' must be one line'; return; }
      values[it.name] = v;
    }
    document.getElementById('go').disabled = true;
    status.textContent = 'Encrypting…';
    const aes = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt']);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const pt = new TextEncoder().encode(JSON.stringify({ items: values }));
    const ct = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(cfg.id) }, aes, pt);
    const wrapped = await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, rsa,
      await crypto.subtle.exportKey('raw', aes));
    const r = await fetch('/api/dropbox/' + encodeURIComponent(cfg.id) + '/submit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wrapped_key: b64(wrapped), iv: b64(iv), ciphertext: b64(ct) }),
    });
    if (r.ok) {
      form.innerHTML = '<p>✅ Sent. The requesting machine will pick it up and install it. You can close this page.</p>';
    } else {
      const err = (await r.json().catch(() => ({}))).error || r.status;
      status.textContent = 'Failed: ' + err;
      document.getElementById('go').disabled = false;
    }
  });
}
`;
