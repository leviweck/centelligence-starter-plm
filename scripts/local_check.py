#!/usr/bin/env python3
"""Run the whole service on your own computer with the real embedding model
(no Modal, no cost): loads ./sample, asks a few questions, looks up contacts.

  pip install -r requirements-dev.txt fastembed
  python scripts/local_check.py [--source sample]
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient  # noqa: E402

from notesearch.api import create_app, git_blob_sha  # noqa: E402
from notesearch.embed import LocalEmbedder  # noqa: E402
from notesearch.index import Index  # noqa: E402
from notesearch.state import State  # noqa: E402
from notesearch.writer import apply_pending  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample"))
    a = ap.parse_args()
    tmp = tempfile.mkdtemp()
    t0 = time.time()
    emb = LocalEmbedder()
    print(f"model {emb.name} loaded in {time.time() - t0:.1f}s")
    st = State(os.path.join(tmp, "state"))
    oi = lambda: Index(os.path.join(tmp, "index.sqlite"), emb)
    tok = "local-check"
    hashes = {"w": {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "scope": "write"}}
    runs = []
    c = TestClient(create_app(oi, st, lambda: runs.append(apply_pending(oi(), st)), hashes))
    H = {"Authorization": f"Bearer {tok}"}
    files = []
    for root, _, names in os.walk(a.source):
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, a.source).replace(os.sep, "/")
            if rel.endswith((".md", ".txt")) or rel == "people/contacts.csv":
                d = open(p, "rb").read()
                files.append({"path": rel, "content_b64": base64.b64encode(d).decode(), "blob_sha": git_blob_sha(d)})
    t0 = time.time()
    print("ingest:", c.post("/v1/ingest", headers=H, json={"files": files}).json(), f"{time.time() - t0:.1f}s")
    print("health:", json.dumps(c.get("/v1/health").json()))
    for q in ["who wants route reports on Mondays?", "when should I plant garlic?",
              "what did Jane ask for after the intro call?", "which startup is raising money?"]:
        r = c.post("/v1/ask", headers=H, json={"question": q}).json()["results"][0]
        print(f"Q: {q}\n   -> {r['path']} [{r['heading']}] contact={r['contact_id']}")
    for key in ["c-jane-d", "S. Sample"]:
        j = c.get(f"/v1/contact/{key}", headers=H).json()
        print(f"contact {key} -> {j['contact_id']} redirected={j['redirected']} merged_from={j['merged_from']}"
              f" excerpts={[e['path'] for e in j['excerpts']]}")
    chk = c.get("/v1/ingest/status", headers=H).json()["contact_checks"]
    print("checks:", json.dumps({k: chk[k] for k in ("orphans", "likely_duplicates", "broken_redirects")}))


if __name__ == "__main__":
    main()
