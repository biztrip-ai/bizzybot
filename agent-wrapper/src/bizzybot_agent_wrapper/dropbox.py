"""`bizzybot-dropbox`: receive secrets on this box through a Central-Dispatch dropbox.

A human pastes values into a one-time web page; their browser encrypts them to
a key pair generated here, Central-Dispatch only relays ciphertext, and this
command decrypts and installs them. Nothing secret is printed.

    bizzybot-dropbox request [--note TEXT] [--restart] SPEC [SPEC ...]

SPEC is one of (append ``::description`` to any to show help text on the page):

    env:NAME               upsert NAME=value into the env file (default
                           /workspaces/env/agent.env if present, else
                           $BIZZYBOT_STATE_DIR/.env; override with --env-file)
    settings:NAME          upsert into $BIZZYBOT_STATE_DIR/settings.env (exported
                           into every Claude session)
    file:NAME:/abs/path    write the value to a file, mode 0600 (multi-line; for
                           keys/PEMs)

Example:

    bizzybot-dropbox request --restart \\
      --note "Tokens for the new BzPM box" \\
      'env:REGISTRATION_TOKEN::48-char token from the BzPM card on the dashboard' \\
      'env:GH_TOKEN::output of `gh auth token`'

It prints the page URL and a check code, waits for the submission (default: the
dropbox's lifetime), installs the values, and prints only names and lengths.
Crypto mirrors central-dispatch/src/dropbox.js: RSA-OAEP(SHA-256) wraps an
AES-256-GCM key; the GCM AAD is the dropbox id.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .agent_wrapper import resolve_central_dispatch
from .paths import state_path

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FILE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def check_code(spki_der: bytes) -> str:
    """Short code derived from the public key; the page computes the same one."""
    d = hashlib.sha256(spki_der).digest()
    s = "".join(CODE_ALPHABET[d[i] % len(CODE_ALPHABET)] for i in range(6))
    return f"{s[:3]}-{s[3:]}"


def parse_spec(spec: str) -> dict:
    body, _, desc = spec.partition("::")
    kind, _, rest = body.partition(":")
    if kind in ("env", "settings"):
        if not ENV_NAME_RE.match(rest):
            raise ValueError(f"bad {kind} name in {spec!r}")
        return {"kind": kind, "name": rest, "multiline": False, "description": desc}
    if kind == "file":
        name, _, path = rest.partition(":")
        if not FILE_NAME_RE.match(name) or not os.path.isabs(path):
            raise ValueError(f"file spec must be file:NAME:/absolute/path, got {spec!r}")
        return {"kind": "file", "name": name, "path": path, "multiline": True, "description": desc}
    raise ValueError(f"unknown spec kind in {spec!r} (want env:, settings:, or file:)")


def default_env_file() -> str:
    cloud = "/workspaces/env/agent.env"
    return cloud if os.path.exists(cloud) else state_path(".env")


def _write_private(path: str, data: str) -> None:
    """Atomically replace `path` with `data`, mode 0600."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".dropbox-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def upsert_env(path: str, name: str, value: str) -> None:
    if re.search(r"[\r\n\x00]", value):
        raise ValueError(f"{name}: env values must be a single line")
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = [ln for ln in f.read().splitlines() if not ln.startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    _write_private(path, "\n".join(lines) + "\n")


def _http(method: str, url: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None


def request(args: argparse.Namespace) -> int:
    try:
        specs = [parse_spec(s) for s in args.specs]
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if len({s["name"] for s in specs}) != len(specs):
        print("error: duplicate names", file=sys.stderr)
        return 2
    central = (args.central_url or resolve_central_dispatch()).rstrip("/")
    env_file = args.env_file or default_env_file()

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    status, resp = _http("POST", f"{central}/api/dropbox", {
        "public_key": base64.b64encode(spki).decode(),
        "items": [{k: s[k] for k in ("name", "multiline", "description")} for s in specs],
        "note": args.note or "",
        "ttl_minutes": args.ttl,
    })
    if status != 200 or not resp:
        print(f"error: create failed ({status}: {resp})", file=sys.stderr)
        return 1

    print(f"Open: {resp['url']}", flush=True)
    print(f"Check code: {check_code(spki)}", flush=True)
    print(f"Waiting for submission (expires in {args.ttl} min)…", flush=True)

    deadline = min(resp["expires_at"] / 1000, time.time() + args.ttl * 60) + 5
    result = None
    while time.time() < deadline:
        status, body = _http(
            "GET", f"{central}/api/dropbox/{resp['id']}/result", token=resp["pickup_secret"]
        )
        if status == 200 and body:
            result = body
            break
        if status not in (204,):
            print(f"error: dropbox {status}: {body}", file=sys.stderr)
            return 1
        time.sleep(args.poll)
    if result is None:
        print("error: timed out waiting for submission", file=sys.stderr)
        return 1

    try:
        aes_key = key.decrypt(
            base64.b64decode(result["wrapped_key"]),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        plaintext = AESGCM(aes_key).decrypt(
            base64.b64decode(result["iv"]),
            base64.b64decode(result["ciphertext"]),
            resp["id"].encode(),
        )
        values = json.loads(plaintext)["items"]
    except Exception as e:  # noqa: BLE001 — never echo the payload
        print(f"error: could not decrypt submission ({type(e).__name__})", file=sys.stderr)
        return 1
    finally:
        del key

    wanted = {s["name"]: s for s in specs}
    extra = set(values) - set(wanted)
    missing = set(wanted) - set(values)
    if extra or missing:
        print(f"error: submission names don't match request "
              f"(missing={sorted(missing)}, unexpected={sorted(extra)}); nothing installed",
              file=sys.stderr)
        return 1

    for name, spec in wanted.items():
        v = values[name]
        if not isinstance(v, str) or v == "":
            print(f"error: {name} is empty; nothing installed", file=sys.stderr)
            return 1
        if not spec["multiline"] and re.search(r"[\r\n\x00]", v):
            print(f"error: {name} must be a single line; nothing installed", file=sys.stderr)
            return 1

    for name, spec in wanted.items():
        v = values[name]
        if spec["kind"] == "env":
            upsert_env(env_file, name, v)
            where = env_file
        elif spec["kind"] == "settings":
            where = state_path("settings.env")
            upsert_env(where, name, v)
        else:
            where = spec["path"]
            _write_private(where, v if v.endswith("\n") else v + "\n")
        print(f"  {name} ({len(v)} chars) → {where}")

    if args.restart:
        rc = subprocess.call(["sudo", "systemctl", "restart", "agent.service"])
        print("restarted agent.service" if rc == 0 else f"restart failed (exit {rc})")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="bizzybot-dropbox", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("request", help="ask a human for secrets and install them here")
    r.add_argument("specs", nargs="+", metavar="SPEC")
    r.add_argument("--note", help="shown at the top of the page")
    r.add_argument("--ttl", type=int, default=30, help="minutes until the link expires (max 120)")
    r.add_argument("--poll", type=float, default=3.0, help="seconds between checks")
    r.add_argument("--env-file", help="target for env: specs")
    r.add_argument("--central-url", help="default: CENTRAL_URL / saved config / hosted")
    r.add_argument("--restart", action="store_true",
                   help="sudo systemctl restart agent.service after installing")
    args = p.parse_args()
    sys.exit(request(args))


if __name__ == "__main__":
    main()
