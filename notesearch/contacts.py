"""Contacts table: parsing, redirects (merges), and data checks.

The table is a CSV with a required contact_id column. Rows are never deleted
by this code. A row is a redirect when its merged_into column holds another
contact_id, or when any cell reads "merged into <contact_id>".
"""
from __future__ import annotations

import csv
import io
import re

COLUMNS = [
    "contact_id", "name", "organization", "role", "type", "reach_by",
    "email", "phone", "stage", "identity", "last_confirmed", "source",
    "note_path", "updated_at", "merged_into",
]
REQUIRED = ["contact_id"]
# Fields that may appear in search text and contact cards. Never email/phone.
CARD_FIELDS = ["name", "organization", "role", "type", "stage", "reach_by"]
PRIVATE_FIELDS = {"email", "phone"}
_MERGED_RE = re.compile(r"merged\s+into\s+([a-z]-[a-z0-9][a-z0-9-]*)", re.I)


class ContactsError(ValueError):
    pass


def parse_csv(text: str) -> list[dict]:
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ContactsError(f"contacts file has no {', '.join(missing)} column")
    rows, seen = [], set()
    for i, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        cid = row.get("contact_id", "")
        if not cid:
            raise ContactsError(f"row {i}: empty contact_id")
        if cid in seen:
            raise ContactsError(f"row {i}: contact_id {cid} appears twice")
        seen.add(cid)
        row["merged_into"] = redirect_target(row)
        rows.append(row)
    return rows


def redirect_target(row: dict) -> str:
    target = (row.get("merged_into") or "").strip()
    if target:
        m = _MERGED_RE.search(target)
        return m.group(1) if m else target
    for key, val in row.items():
        if key == "contact_id" or not val:
            continue
        m = _MERGED_RE.search(val)
        if m:
            return m.group(1)
    return ""


def resolve(contact_id: str, by_id: dict[str, dict]) -> tuple[str | None, list[str]]:
    """Follow merged_into links. Returns (survivor_id or None, chain)."""
    chain, cur = [], contact_id
    while cur in by_id:
        if cur in chain:
            return None, chain + [cur]  # loop
        chain.append(cur)
        nxt = by_id[cur].get("merged_into", "")
        if not nxt:
            return cur, chain
        cur = nxt
    return None, chain + [cur]  # points at an unknown id


def card_text(row: dict) -> str:
    parts = [f"{row.get('name', '')} ({row['contact_id']})"]
    for f in CARD_FIELDS[1:]:
        if row.get(f):
            parts.append(f"{f}: {row[f]}")
    return ". ".join(parts)


def public_card(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in PRIVATE_FIELDS}


def _norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    return d[-10:] if len(d) >= 10 else d


def checks(rows: list[dict], note_paths: set[str], note_contact_ids: dict[str, str]) -> dict:
    """note_contact_ids maps note path -> contact_id named in its header."""
    by_id = {r["contact_id"]: r for r in rows}
    active = [r for r in rows if not r.get("merged_into")]
    orphans = []
    for r in active:
        np = r.get("note_path", "")
        if not np:
            orphans.append({"contact_id": r["contact_id"], "problem": "no note_path"})
        elif np not in note_paths:
            orphans.append({"contact_id": r["contact_id"], "problem": f"note not found: {np}"})
    for path, cid in sorted(note_contact_ids.items()):
        if cid not in by_id:
            orphans.append({"note_path": path, "problem": f"note names unknown contact_id {cid}"})
    dupes, groups = [], {}
    for r in active:
        keys = []
        if r.get("email"):
            keys.append(("email", r["email"].lower()))
        if r.get("phone") and len(_norm_phone(r["phone"])) >= 7:
            keys.append(("phone", _norm_phone(r["phone"])))
        if r.get("name") and r.get("organization"):
            keys.append(("name+organization", (r["name"].lower(), r["organization"].lower())))
        for k in keys:
            groups.setdefault(k, []).append(r["contact_id"])
    for (kind, _), ids in sorted(groups.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        if len(ids) > 1:
            dupes.append({"match_on": kind, "contact_ids": sorted(ids)})
    broken = []
    for r in rows:
        if r.get("merged_into"):
            survivor, chain = resolve(r["contact_id"], by_id)
            if survivor is None:
                broken.append({"contact_id": r["contact_id"], "chain": chain})
    return {
        "rows": len(rows), "active": len(active), "redirects": len(rows) - len(active),
        "orphans": orphans, "likely_duplicates": dupes, "broken_redirects": broken,
    }
