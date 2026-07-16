// Signed, stateless session cookie (HMAC-SHA256). No session store, no
// dependency — the cookie carries { teamId, userId, name, exp } and its
// signature, which we verify on each request.
import crypto from 'node:crypto';
import { config } from './config.js';

const COOKIE = 'cb_session';
const MAX_AGE_S = 30 * 24 * 3600; // 30 days

function sign(payload) {
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const mac = crypto.createHmac('sha256', config.sessionSecret).update(body).digest('base64url');
  return `${body}.${mac}`;
}

function verify(token) {
  if (!token || !token.includes('.')) return null;
  const [body, mac] = token.split('.');
  const expected = crypto.createHmac('sha256', config.sessionSecret).update(body).digest('base64url');
  const a = Buffer.from(mac);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  let data;
  try {
    data = JSON.parse(Buffer.from(body, 'base64url').toString());
  } catch {
    return null;
  }
  if (!data.exp || data.exp < Date.now()) return null;
  return data;
}

function parseCookies(req) {
  const out = {};
  for (const part of (req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function cookieAttrs(value, maxAge) {
  const attrs = [`${COOKIE}=${value}`, 'HttpOnly', 'Path=/', `Max-Age=${maxAge}`, 'SameSite=Lax'];
  if (config.cookieSecure) attrs.push('Secure');
  return attrs.join('; ');
}

export function setSession(res, data) {
  const token = sign({ ...data, exp: Date.now() + MAX_AGE_S * 1000 });
  res.append('Set-Cookie', cookieAttrs(token, MAX_AGE_S));
}

export function clearSession(res) {
  res.append('Set-Cookie', cookieAttrs('', 0));
}

export function getSession(req) {
  return verify(parseCookies(req)[COOKIE]);
}
