// Self-signed localhost certificate for local HTTPS (so Slack's OAuth redirect
// to https://localhost resolves). Generated once via openssl; gitignored.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

export function ensureSelfSignedCert(dir) {
  fs.mkdirSync(dir, { recursive: true });
  const keyPath = path.join(dir, 'localhost.key');
  const certPath = path.join(dir, 'localhost.crt');

  if (!fs.existsSync(keyPath) || !fs.existsSync(certPath)) {
    execFileSync(
      'openssl',
      [
        'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', keyPath,
        '-out', certPath,
        '-days', '825',
        '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
      ],
      { stdio: 'ignore' },
    );
    console.log(`[central-dispatch] generated self-signed cert in ${dir}`);
  }
  return { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) };
}
