"""The Writer applies accepted ingest requests to the index.

Single-writer design: only the Writer changes the index file. The API only
adds request files to the pending folder; the Writer remembers the last
request it applied (meta 'applied_through'), so the two never write the
same file.
"""
from __future__ import annotations

import base64
import json
import os
import time

from . import contacts as ct
from .index import Index
from .state import State


def _meta_get(ix: Index, key, default):
    r = ix.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def _meta_set(ix: Index, key, value):
    ix.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(value)))


def held(ix: Index) -> dict:
    return _meta_get(ix, "held", {"deletes": [], "contacts_csv": None, "reasons": []})


def apply_pending(ix: Index, st: State, kind: str = "run") -> dict:
    s = ix.s
    done = _meta_get(ix, "applied_through", "")
    hold = held(ix)
    summary = {"kind": kind, "requests": 0, "notes_indexed": 0, "chunks_added": 0,
               "notes_deleted": 0, "contacts": None, "held": []}
    for path in st.pending_files():
        rid = os.path.basename(path)[:-5]
        if rid <= done:
            continue
        with open(path) as f:
            req = json.load(f)
        summary["requests"] += 1
        # releases of held items (owner said yes)
        for what in req.get("release", []):
            if what == "deletes":
                for p in hold["deletes"]:
                    summary["notes_deleted"] += int(ix.delete_note(p))
                hold["deletes"] = []
            if what == "contacts" and hold.get("contacts_csv"):
                summary["contacts"] = ix.load_contacts(ct.parse_csv(hold["contacts_csv"]))
                hold["contacts_csv"] = None
        for fobj in req.get("files", []):
            text = base64.b64decode(fobj["content_b64"]).decode("utf-8", errors="replace")
            if fobj["path"] == s.contacts_path:
                rows = ct.parse_csv(text)
                before = ix.counts()["contacts_active"] + ix.counts()["contacts_redirects"]
                drop = before - len({r["contact_id"] for r in rows} & {r["contact_id"] for r in ix.contact_rows()})
                if before >= 10 and drop * 100 > before * s.max_contact_drop_pct:
                    hold["contacts_csv"] = text
                    hold["reasons"].append(f"contacts file drops {drop} of {before} rows")
                    summary["held"].append("contacts")
                else:
                    summary["contacts"] = ix.load_contacts(rows)
                continue
            n = ix.upsert_note(fobj["path"], text, fobj["blob_sha"])
            summary["chunks_added"] += n
            summary["notes_indexed"] += int(n > 0)
        deletes = list(req.get("deletes", []))
        if len(deletes) > s.max_deletes_per_request:
            hold["deletes"] = sorted(set(hold["deletes"]) | set(deletes))
            hold["reasons"].append(f"{len(deletes)} deletes in one request")
            summary["held"].append("deletes")
            deletes = []
        if req.get("manifest") is not None:
            age_h = (time.time() - req.get("manifest_generated_at", 0)) / 3600
            missing = sorted(ix.note_paths() - set(req["manifest"]))
            total = max(1, len(ix.note_paths()))
            if age_h > 26:
                hold["reasons"].append("manifest older than 26 hours; no deletes")
            elif len(missing) * 100 > total * s.max_manifest_missing_pct:
                hold["deletes"] = sorted(set(hold["deletes"]) | set(missing))
                hold["reasons"].append(f"{len(missing)} of {total} notes missing from manifest")
                summary["held"].append("manifest_deletes")
            else:
                deletes += missing
        for p in deletes:
            summary["notes_deleted"] += int(ix.delete_note(p))
        done = rid
    hold["reasons"] = hold["reasons"][-20:]
    _meta_set(ix, "applied_through", done)
    _meta_set(ix, "held", hold)
    summary["finished_at"] = time.time()
    _meta_set(ix, "last_" + kind, summary)
    ix.commit()
    return summary
