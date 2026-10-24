#!/usr/bin/env python3
"""Make your access tokens and upload only their fingerprints to Modal.

  python scripts/admin.py make-tokens --app notes-search [--dry-run]

Creates two random tokens (read, write), saves each in its own file with
permission 600 under ~/.config/<app>/, and stores only their SHA-256 values in
the Modal Secret <app>-tokens (key NS_TOKEN_HASHES). Never prints a token.
Running it again makes NEW tokens; the old ones stop working after you redeploy
or the API container restarts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile


def make_tokens(app: str, dry_run: bool, config_root: str = "~/.config") -> dict:
    folder = os.path.expanduser(os.path.join(config_root, app))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    hashes = {}
    for name in ("read", "write"):
        tok = secrets.token_urlsafe(32)
        path = os.path.join(folder, f"{name}.token")
        if not dry_run:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(tok + "\n")
        hashes[name] = {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "scope": name}
        print(f"{name} token -> {path}" + (" (dry run, not saved)" if dry_run else ""))
    return hashes


def upload(app: str, hashes: dict, dry_run: bool):
    name = f"{app}-tokens"
    if dry_run:
        print(f"would run: modal secret create {name} --from-json <temp file> --force")
        return
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"NS_TOKEN_HASHES": json.dumps(hashes)}, f)
        subprocess.run(["modal", "secret", "create", name, "--from-json", tmp, "--force"],
                       check=True, stdout=subprocess.DEVNULL)
        print(f"uploaded fingerprints to Modal Secret {name}")
    finally:
        os.remove(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make-tokens")
    mk.add_argument("--app", default="notes-search")
    mk.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "make-tokens":
        upload(a.app, make_tokens(a.app, a.dry_run), a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
