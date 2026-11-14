#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Local NeoKikoeru activation helper (standalone, standard library only).

Use this when a patched client is already running but `/api/sysinfo` still says
`activated:false`, which makes the work file lists come back empty.

Usage:
    python .\activate_local.py
    python .\activate_local.py --base http://127.0.0.1:5233
    python .\activate_local.py --data-dir C:\Users\me\AppData\Roaming\neokikoeru
    python .\activate_local.py --user admin --password ******

By default the script signs a short-lived admin token with `auth.jwt_secret`
from the client's own config.json; that needs no password but requires running
on the same machine as the client. Without a readable config.json it falls back
to a username/password login.

After a successful activation it queries /api/sysinfo and prints the state.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_LICENSE_KEY = "Hello-World"


def _b64u(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _jwt(secret: str, user_id: int = 1, ttl: int = 3600) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"},
                              separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64u(json.dumps({"id": user_id, "exp": now + ttl, "iat": now},
                               separators=(",", ":")).encode())
    sig = _b64u(hmac.new(secret.encode(), header + b"." + payload,
                         hashlib.sha256).digest())
    return (header + b"." + payload + b"." + sig).decode()


def http(method: str, url: str, body=None, token: str = "", timeout: int = 20):
    req = urllib.request.Request(url, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "neokikoeru"
    return Path.home() / ".config" / "neokikoeru"


def find_config(given: str) -> Path | None:
    candidates = []
    if given:
        p = Path(given).expanduser()
        candidates += [p, p / "config.json"]
    candidates += [default_data_dir() / "config.json",
                   Path.cwd() / "config.json"]
    for p in candidates:
        if p.is_file():
            return p
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Activate a locally running patched NeoKikoeru")
    ap.add_argument("--base", default="http://127.0.0.1:5233", help="client URL")
    ap.add_argument("--data-dir", default="", help="client data dir (holds config.json)")
    ap.add_argument("--user", default="", help="admin user (only without config.json)")
    ap.add_argument("--password", default=os.environ.get("NK_ADMIN_PASSWORD", ""),
                    help="admin password (default: NK_ADMIN_PASSWORD)")
    ap.add_argument("--license-key", default=DEFAULT_LICENSE_KEY)
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    token = ""
    user = args.user or "admin"
    cfg = find_config(args.data_dir)
    if cfg:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            secret = (data.get("auth") or {}).get("jwt_secret", "")
            if secret:
                token = _jwt(secret)
                print("[i] signed an admin token from %s" % cfg)
        except Exception as e:  # noqa: BLE001
            print("[!] cannot read %s: %s" % (cfg, e), file=sys.stderr)

    if not token:
        if not args.password:
            print("[!] no usable config.json and no --password", file=sys.stderr)
            print("    point --data-dir at the client data dir, or pass --user/--password",
                  file=sys.stderr)
            return 2
        code, body = http("POST", base + "/api/auth/login",
                          {"username": user, "password": args.password})
        if code != 200:
            print("[!] login failed HTTP %s: %s" % (code, body[:300]), file=sys.stderr)
            return 1
        try:
            token = json.loads(body).get("token", "")
        except Exception:  # noqa: BLE001
            token = ""
        if not token:
            print("[!] login response carries no token", file=sys.stderr)
            return 1
        print("[i] logged in as %s" % user)

    code, body = http("POST", base + "/api/admin/license/activate",
                      {"license_key": args.license_key}, token)
    if code not in (200, 204):
        print("[!] activation failed HTTP %s: %s" % (code, body[:500]), file=sys.stderr)
        return 1

    time.sleep(1)
    code, body = http("GET", base + "/api/sysinfo")
    try:
        info = json.loads(body)
    except Exception:  # noqa: BLE001
        info = {}
    activated = bool((info.get("license") or {}).get("activated"))
    print("[%s] activated=%s  %s" % ("OK" if activated else "!", activated,
                                     json.dumps(info.get("license") or {},
                                                ensure_ascii=False)))
    if activated:
        print("     reload the work detail page to see the file list.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
