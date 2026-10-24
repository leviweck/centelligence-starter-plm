#!/usr/bin/env python3
"""Send changed notes (and the contacts file) to your notes search service.

Standard library only. Runs on your own computer.

  python scripts/push.py --source ~/notes --url https://<your-app>.modal.run \
      --token-file ~/.config/notes-search/write.token [--dry-run]

Source can be a git repository (default when it has .git: files are read from
the last commit, and only what changed since the last successful send goes) or
a plain folder (changes found by content fingerprint).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SUFFIXES = (".md", ".txt")
MAX_BATCH_BYTES = 900_000
MAX_BATCH_FILES = 150


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git(src, *args) -> bytes:
    return subprocess.run(["git", "-C", src, *args], check=True, capture_output=True).stdout


# Folders skipped when --include is not given: the PLM's own code folder, so its
# README and fake samples are never sent as your notes. --include overrides this.
DEFAULT_SKIP = ("plm",)


def _under(path: str, folder: str) -> bool:
    folder = folder.strip("/")
    return path == folder or path.startswith(folder + "/")


def wanted(path: str, include: list[str], contacts: str) -> bool:
    if path == contacts:
        return True
    if not path.endswith(SUFFIXES):
        return False
    if include:
        return any(_under(path, i) for i in include)
    return not any(_under(path, d) for d in DEFAULT_SKIP)


def changes_git(src, state, include, contacts, full):
    head = git(src, "rev-parse", "HEAD").decode().strip()
    last = None if full else state.get("last_commit")
    files, deletes = [], []
    if last:
        out = git(src, "diff", "--name-status", "--no-renames", "-z", last, head).decode()
        parts = out.split("\0")
        for status, path in zip(parts[0::2], parts[1::2]):
            if not wanted(path, include, contacts):
                continue
            (deletes if status == "D" else files).append(path)
    else:
        files = [p for p in git(src, "ls-tree", "-r", "--name-only", "-z", head).decode().split("\0")
                 if p and wanted(p, include, contacts)]
    read = lambda p: git(src, "show", f"{head}:{p}")
    return head, files, deletes, read


def all_folder_files(src, include, contacts):
    out = []
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), src).replace(os.sep, "/")
            if wanted(rel, include, contacts):
                out.append(rel)
    return sorted(out)


def changes_folder(src, state, include, contacts, full):
    known = {} if full else state.get("blobs", {})
    now = {}
    read = lambda p: open(os.path.join(src, p), "rb").read()
    for p in all_folder_files(src, include, contacts):
        now[p] = blob_sha(read(p))
    files = [p for p, h in now.items() if known.get(p) != h]
    deletes = [p for p in known if p not in now]
    return now, files, deletes, read


def post(url, token, body, path="/v1/ingest"):
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def send(url, token, body):
    for attempt in range(4):
        code, res = post(url, token, body)
        if code == 429:
            time.sleep(min(int(res.get("retry_after_s", 60)), 600) if attempt < 3 else 0)
            continue
        return code, res
    return code, res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="notes folder or git repository")
    ap.add_argument("--mode", choices=["auto", "git", "folder"], default="auto")
    ap.add_argument("--include", default="", help="comma-separated folders to send (default: all except plm/)")
    ap.add_argument("--contacts", default="people/contacts.csv")
    ap.add_argument("--url", default=os.environ.get("NS_URL", ""))
    ap.add_argument("--token-file", default=os.environ.get("NS_WRITE_TOKEN_FILE", ""))
    ap.add_argument("--state-file", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true", help="resend everything")
    ap.add_argument("--manifest", action="store_true", help="also send the full file list (removes deleted notes)")
    ap.add_argument("--release", choices=["deletes", "contacts"], help="apply held items (after you checked them)")
    ap.add_argument("--no-wait", action="store_true", help="don't wait out the 10-minute gap")
    a = ap.parse_args(argv)
    src = os.path.abspath(os.path.expanduser(a.source))
    include = [i.strip() for i in a.include.split(",") if i.strip()]
    mode = a.mode if a.mode != "auto" else ("git" if os.path.isdir(os.path.join(src, ".git")) else "folder")
    state_file = a.state_file or os.path.join(
        os.path.expanduser("~/.config/notes-search"),
        "push-state-" + hashlib.sha256(src.encode()).hexdigest()[:12] + ".json")
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}

    if mode == "git":
        marker, files, deletes, read = changes_git(src, state, include, a.contacts, a.full)
    else:
        marker, files, deletes, read = changes_folder(src, state, include, a.contacts, a.full)
    manifest = None
    if a.manifest:
        manifest = ([p for p in git(src, "ls-tree", "-r", "--name-only", "-z", "HEAD").decode().split("\0")
                     if p and wanted(p, include, a.contacts) and p != a.contacts] if mode == "git"
                    else [p for p in all_folder_files(src, include, a.contacts) if p != a.contacts])
    blobs = {p: read(p) for p in files}
    size = sum(len(b) for b in blobs.values())
    print(f"mode={mode} files={len(files)} bytes={size} deletes={len(deletes)}"
          + (f" manifest={len(manifest)}" if manifest is not None else ""))
    if a.dry_run:
        for p in files[:50]:
            print("  send", p)
        for p in deletes[:50]:
            print("  delete", p)
        return 0
    if not a.url or not a.token_file:
        sys.exit("need --url and --token-file (or NS_URL / NS_WRITE_TOKEN_FILE)")
    token = open(os.path.expanduser(a.token_file)).read().strip()

    batches, cur, cur_size = [], [], 0
    for p in files:
        b = blobs[p]
        enc = len(b) * 4 // 3 + 200
        if enc > MAX_BATCH_BYTES:
            print(f"  skip (too big for one request): {p}")
            continue
        if cur and (cur_size + enc > MAX_BATCH_BYTES or len(cur) >= MAX_BATCH_FILES):
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append({"path": p, "content_b64": base64.b64encode(b).decode(), "blob_sha": blob_sha(b)})
        cur_size += enc
    if cur or not batches:
        batches.append(cur)
    last = {}
    for i, batch in enumerate(batches):
        body = {"files": batch}
        if i == len(batches) - 1:
            if deletes:
                body["deletes"] = deletes
            if manifest is not None:
                body["manifest"], body["manifest_generated_at"] = manifest, time.time()
            if a.release:
                body["release"] = [a.release]
        if not any(body.values()):
            print("nothing to send")
            break
        code, last = send(a.url, token, body)
        print(f"batch {i + 1}/{len(batches)}: HTTP {code} {last.get('status', last.get('detail'))}")
        if code != 200:
            print("not sent; nothing marked as done, so the next run retries")
            return 1
    if mode == "git":
        state["last_commit"] = marker
    else:
        state["blobs"] = marker
    json.dump(state, open(state_file, "w"), indent=1)
    if last.get("status") == "waiting_for_gap" and not a.no_wait:
        wait = int(last.get("retry_after_s", 600)) + 5
        print(f"waiting {wait}s for the run gap, then asking for a run")
        time.sleep(wait)
        code, res = send(a.url, token, {})
        print(f"kick: HTTP {code} {res.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
