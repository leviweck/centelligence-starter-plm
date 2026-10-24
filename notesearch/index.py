"""The index: one SQLite file with chunks, their embeddings, a keyword (FTS5)
index, the contacts table, and note metadata. Search mixes meaning and
keywords with reciprocal rank fusion."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

import numpy as np

from . import contacts as ct
from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
  path TEXT PRIMARY KEY, blob_sha TEXT, contact_id TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, kind TEXT, path TEXT, heading TEXT, text TEXT,
  contact_id TEXT, embedding BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS chunks_contact ON chunks(contact_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, heading);
CREATE TABLE IF NOT EXISTS contacts(
  contact_id TEXT PRIMARY KEY, row_json TEXT, content_hash TEXT,
  active INTEGER, in_file INTEGER, updated_at REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


def parse_note(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). Only simple key: value lines."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip("'\"")
    return fm, text[m.end():]


def chunk_text(body: str, size: int, overlap: int) -> list[tuple[str, str]]:
    """Split on headings, then into size-character windows with overlap."""
    sections, last, heading = [], 0, ""
    for m in _HEAD_RE.finditer(body):
        if body[last:m.start()].strip():
            sections.append((heading, body[last:m.start()]))
        heading, last = m.group(2).strip(), m.end()
    if body[last:].strip():
        sections.append((heading, body[last:]))
    out = []
    step = max(1, size - overlap)
    for h, txt in sections:
        txt = txt.strip()
        for i in range(0, len(txt), step):
            piece = txt[i:i + size].strip()
            if piece:
                out.append((h, piece))
            if i + size >= len(txt):
                break
    return out


def _fts_query(q: str) -> str:
    toks = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in toks[:32])


class Index:
    def __init__(self, path: str, embedder, settings: Settings | None = None, readonly: bool = False):
        import os
        if readonly and os.path.exists(path):
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:  # readonly + no file yet: an empty in-memory index
            self.db = sqlite3.connect(":memory:" if readonly else path)
        self.db.row_factory = sqlite3.Row
        if not (readonly and path and self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='chunks'").fetchone()):
            self.db.executescript(SCHEMA)
        self.emb = embedder
        self.s = settings or Settings()

    def close(self):
        self.db.close()

    # ---------- writes ----------
    def _add_chunks(self, kind, path, pieces, contact_id):
        if not pieces:
            return 0
        texts = [(f"{h}\n{t}" if h else t) for h, t in pieces]
        vecs = self.emb.embed_documents(texts)
        for (h, t), v in zip(pieces, vecs):
            cur = self.db.execute(
                "INSERT INTO chunks(kind,path,heading,text,contact_id,embedding) VALUES(?,?,?,?,?,?)",
                (kind, path, h, t, contact_id, np.asarray(v, dtype=np.float32).tobytes()))
            self.db.execute("INSERT INTO chunks_fts(rowid,text,heading) VALUES(?,?,?)",
                            (cur.lastrowid, t, h))
        return len(pieces)

    def _drop_chunks(self, where: str, args: tuple):
        ids = [r[0] for r in self.db.execute(f"SELECT id FROM chunks WHERE {where}", args)]
        for i in ids:
            self.db.execute("DELETE FROM chunks_fts WHERE rowid=?", (i,))
        self.db.execute(f"DELETE FROM chunks WHERE {where}", args)

    def upsert_note(self, path: str, text: str, blob_sha: str) -> int:
        row = self.db.execute("SELECT blob_sha FROM notes WHERE path=?", (path,)).fetchone()
        if row and row["blob_sha"] == blob_sha:
            return 0
        fm, body = parse_note(text)
        cid = fm.get("contact_id") or None
        self._drop_chunks("path=? AND kind='note'", (path,))
        n = self._add_chunks("note", path, chunk_text(body, self.s.chunk_chars, self.s.chunk_overlap), cid)
        self.db.execute("INSERT OR REPLACE INTO notes(path,blob_sha,contact_id,updated_at) VALUES(?,?,?,?)",
                        (path, blob_sha, cid, time.time()))
        return n

    def delete_note(self, path: str) -> bool:
        self._drop_chunks("path=? AND kind='note'", (path,))
        return self.db.execute("DELETE FROM notes WHERE path=?", (path,)).rowcount > 0

    def note_paths(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT path FROM notes")}

    def load_contacts(self, rows: list[dict]) -> dict:
        """Upsert the full contacts table. Rows missing from the file are kept
        (in_file=0) and drop out of search; nothing is deleted."""
        now, embedded, unchanged = time.time(), 0, 0
        ids = {r["contact_id"] for r in rows}
        for r in rows:
            h = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
            old = self.db.execute("SELECT content_hash,in_file FROM contacts WHERE contact_id=?",
                                  (r["contact_id"],)).fetchone()
            active = 0 if r.get("merged_into") else 1
            if old and old["content_hash"] == h and old["in_file"] == 1:
                unchanged += 1
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO contacts(contact_id,row_json,content_hash,active,in_file,updated_at)"
                " VALUES(?,?,?,?,1,?)", (r["contact_id"], json.dumps(r), h, active, now))
            self._drop_chunks("kind='contact' AND contact_id=?", (r["contact_id"],))
            if active:
                embedded += self._add_chunks("contact", r.get("note_path") or f"contacts:{r['contact_id']}",
                                             [("contact", ct.card_text(r))], r["contact_id"])
        gone = [x[0] for x in self.db.execute("SELECT contact_id FROM contacts WHERE in_file=1")
                if x[0] not in ids]
        for cid in gone:
            self.db.execute("UPDATE contacts SET in_file=0, updated_at=? WHERE contact_id=?", (now, cid))
            self._drop_chunks("kind='contact' AND contact_id=?", (cid,))
        return {"rows": len(rows), "embedded": embedded, "unchanged": unchanged, "left_file": len(gone)}

    def contact_rows(self, include_gone=False) -> list[dict]:
        q = "SELECT row_json FROM contacts" + ("" if include_gone else " WHERE in_file=1")
        return [json.loads(r[0]) for r in self.db.execute(q)]

    def commit(self):
        self.db.commit()

    # ---------- reads ----------
    def counts(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]
        return {
            "notes": one("SELECT COUNT(*) FROM notes"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "contacts_active": one("SELECT COUNT(*) FROM contacts WHERE in_file=1 AND active=1"),
            "contacts_redirects": one("SELECT COUNT(*) FROM contacts WHERE in_file=1 AND active=0"),
        }

    def contact_checks(self) -> dict:
        notes = {r[0]: r[1] for r in self.db.execute("SELECT path, contact_id FROM notes")}
        return ct.checks(self.contact_rows(), set(notes), {p: c for p, c in notes.items() if c})

    def _by_id(self) -> dict:
        return {r["contact_id"]: r for r in self.contact_rows()}

    def search(self, question: str, top_k: int | None = None, contact_id: str | None = None) -> list[dict]:
        k = top_k or self.s.top_k
        where, args = "", ()
        if contact_id:
            where, args = " WHERE contact_id=?", (contact_id,)
        rows = self.db.execute(f"SELECT id, embedding FROM chunks{where}", args).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        sims = mat @ self.emb.embed_query(question)
        vec_rank = [ids[i] for i in np.argsort(-sims)[:50]]
        kw_rank = []
        fq = _fts_query(question)
        if fq:
            sql = "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT 50"
            allowed = set(ids)
            kw_rank = [r[0] for r in self.db.execute(sql, (fq,)) if r[0] in allowed]
        scores: dict[int, float] = {}
        for rank_list in (vec_rank, kw_rank):
            for pos, cid in enumerate(rank_list):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + pos + 1)
        best = sorted(scores, key=lambda i: -scores[i])[:k]
        by_id = self._by_id()
        out = []
        for cid in best:
            r = self.db.execute("SELECT kind,path,heading,text,contact_id FROM chunks WHERE id=?", (cid,)).fetchone()
            hit = {"kind": r["kind"], "path": r["path"], "heading": r["heading"],
                   "excerpt": r["text"], "contact_id": r["contact_id"], "score": round(scores[cid], 5)}
            if r["contact_id"]:
                survivor, _ = ct.resolve(r["contact_id"], by_id)
                if survivor:
                    hit["contact"] = ct.public_card(by_id[survivor])
            out.append(hit)
        return out

    def find_contact(self, key: str) -> str | None:
        by_id = self._by_id()
        if key in by_id:
            return key
        low = key.strip().lower()
        for r in by_id.values():
            if r.get("name", "").lower() == low:
                return r["contact_id"]
        return None

    def contact_lookup(self, key: str, excerpts: int = 3) -> dict | None:
        by_id = self._by_id()
        cid = self.find_contact(key)
        if not cid:
            return None
        survivor, chain = ct.resolve(cid, by_id)
        if not survivor:
            return {"contact_id": cid, "error": "broken redirect", "chain": chain}
        merged_from = sorted(i for i in by_id if i != survivor and ct.resolve(i, by_id)[0] == survivor)
        card = ct.public_card(by_id[survivor])
        hits = [h for h in self.search(card.get("name") or survivor, top_k=excerpts * 2, contact_id=survivor)
                if h["kind"] == "note"][:excerpts]
        return {"requested": key, "contact_id": survivor, "redirected": survivor != cid,
                "merged_from": merged_from, "card": card, "note_path": card.get("note_path"),
                "excerpts": [{k: h[k] for k in ("path", "heading", "excerpt")} for h in hits]}
