import http from 'node:http';
import https from 'node:https';
import express from 'express';
import { config } from './config.js';
import { init } from './db.js';
import { router } from './routes.js';
import { attachWsHub } from './wsHub.js';
import { ensureSelfSignedCert } from './cert.js';

const app = express();

// Capture the raw body so we can verify Slack signatures.
app.use(
  express.json({
    verify: (req, _res, buf) => {
      req.rawBody = buf.toString();
    },
  }),
);

app.get('/health', (_req, res) => res.json({ ok: true }));
app.use(router);

await init(); // create tables / schema before serving

const server = config.tlsSelfSigned
  ? https.createServer(ensureSelfSignedCert(config.certDir), app)
  : http.createServer(app);
attachWsHub(server);

server.listen(config.port, () => {
  const scheme = config.tlsSelfSigned ? 'https' : 'http';
  console.log(
    `[central] listening (${scheme}) on ${config.publicUrl} (port ${config.port})`,
  );
});
