"""HTTP API (FastAPI). Works the same on Modal and locally in tests.

Tokens: callers send "Authorization: Bearer <token>". The server only holds a
SHA-256 fingerprint of each token, in a JSON map:
  {"<token name>": {"sha256": "<hex>", "scope": "read" | "write"}}
A write token can also read.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from . import contacts as ct
from .config import Settings
from .state import State
from .writer import _meta_get, held


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def check_token(header: str | None, hashes: dict, need: str) -> str:
    if not header or not header.lower().startswith("bearer "):
        raise HTTPException(401, "missing token")
    digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    for name, entry in hashes.items():
        if hmac.compare_digest(digest, entry.get("sha256", "")):
            if need == "write" and entry.get("scope") != "write":
                raise HTTPException(403, "this token can only read")
            return name
    raise HTTPException(401, "unknown token")


def _safe_path(p: str, s: Settings) -> bool:
    if not p or p.startswith("/") or "\\" in p or ".." in p.split("/"):
        return False
    return p == s.contacts_path or p.endswith(tuple(s.note_suffixes))


def create_app(open_index: Callable, state: State, trigger_writer: Callable[[], None],
               token_hashes: dict, settings: Settings | None = None,
               after_state_write: Callable[[], None] = lambda: None) -> FastAPI:
    s = settings or Settings()
    app = FastAPI(title="notes search", version=__version__, docs_url=None, redoc_url=None)

    def with_index(fn):
        ix = open_index()
        try:
            return fn(ix)
        finally:
            ix.close()

    @app.get("/v1/health")
    def health():
        def f(ix):
            gate, reason, wait = state.run_gate()
            return {"status": "up", "version": __version__, **ix.counts(),
                    "runs_today": state.runs_today(), "max_runs_per_day": s.max_runs_per_day,
                    "day_timezone": s.timezone, "next_run_allowed": gate,
                    "last_run_finished_at": (_meta_get(ix, "last_run", {}) or {}).get("finished_at"),
                    "last_nightly_finished_at": (_meta_get(ix, "last_nightly", {}) or {}).get("finished_at"),
                    "held_items": bool(held(ix)["deletes"] or held(ix)["contacts_csv"])}
        return with_index(f)

    @app.post("/v1/ask")
    async def ask(request: Request):
        check_token(request.headers.get("authorization"), token_hashes, "read")
        body = await request.json()
        q = str(body.get("question", "")).strip()
        if not q or len(q) > s.max_question_chars:
            raise HTTPException(400, f"question must be 1-{s.max_question_chars} characters")
        k = min(int(body.get("top_k", s.top_k)), 20)
        return with_index(lambda ix: {"question": q, "results": ix.search(q, top_k=k)})

    @app.get("/v1/contact/{key}")
    def contact(key: str, request: Request):
        check_token(request.headers.get("authorization"), token_hashes, "read")
        res = with_index(lambda ix: ix.contact_lookup(key))
        if res is None:
            raise HTTPException(404, "no contact with that id or name")
        return res

    @app.post("/v1/ingest")
    async def ingest(request: Request):
        name = check_token(request.headers.get("authorization"), token_hashes, "write")
        raw = await request.body()
        if len(raw) > s.max_request_bytes:
            raise HTTPException(413, f"request over {s.max_request_bytes} bytes; send fewer files")
        if not state.rate_ok(name):
            after_state_write()
            return JSONResponse({"status": "rate_limited", "retry_after_s": 300}, status_code=429)
        body = json.loads(raw or b"{}")
        files = body.get("files", [])
        if len(files) > s.max_files_per_request:
            raise HTTPException(400, f"more than {s.max_files_per_request} files in one request")
        for fobj in files:
            p = fobj.get("path", "")
            if not _safe_path(p, s):
                raise HTTPException(400, f"path not accepted: {p}")
            data = base64.b64decode(fobj.get("content_b64", ""))
            if git_blob_sha(data) != fobj.get("blob_sha"):
                raise HTTPException(400, f"blob id does not match content: {p}")
            if p == s.contacts_path:
                try:
                    ct.parse_csv(data.decode("utf-8"))
                except (ct.ContactsError, UnicodeDecodeError) as e:
                    raise HTTPException(400, f"contacts file refused: {e}")
        for p in body.get("deletes", []):
            if not _safe_path(p, s):
                raise HTTPException(400, f"delete path not accepted: {p}")
        payload = {k: body[k] for k in ("files", "deletes", "manifest", "manifest_generated_at", "release")
                   if k in body}
        rid = state.add_pending(payload) if payload else None
        gate, reason, wait = state.run_gate()
        if gate:
            state.record_run("run", {"trigger_request": rid})
            after_state_write()
            trigger_writer()
            return {"status": "accepted_running", "request_id": rid}
        after_state_write()
        if reason == "min_gap":
            return {"status": "waiting_for_gap", "request_id": rid, "retry_after_s": wait}
        return {"status": "queued_for_scheduled_run", "request_id": rid,
                "note": "today's run cap is used; the nightly run applies this"}

    @app.get("/v1/ingest/status")
    def status(request: Request):
        check_token(request.headers.get("authorization"), token_hashes, "write")

        def f(ix):
            done = _meta_get(ix, "applied_through", "")
            pending = [p for p in state.pending_files() if p.rsplit("/", 1)[-1][:-5] > done]
            gate, reason, wait = state.run_gate()
            return {"pending_requests": len(pending), "applied_through": done,
                    "runs_today": state.runs_today(), "max_runs_per_day": s.max_runs_per_day,
                    "next_run_allowed": gate, "reason": reason, "retry_after_s": wait,
                    "held": {"deletes": held(ix)["deletes"], "contacts": bool(held(ix)["contacts_csv"]),
                             "reasons": held(ix)["reasons"]},
                    "counts": ix.counts(), "contact_checks": ix.contact_checks(),
                    "last_run": _meta_get(ix, "last_run", None),
                    "last_nightly": _meta_get(ix, "last_nightly", None)}
        return with_index(f)

    return app
