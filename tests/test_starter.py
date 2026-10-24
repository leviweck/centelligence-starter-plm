"""Offline tests: fake embeddings, no network, no Modal. Run: pytest -q"""
import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from notesearch import contacts as ct
from notesearch.api import create_app, git_blob_sha
from notesearch.config import Settings
from notesearch.embed import FakeEmbedder
from notesearch.index import Index, chunk_text, parse_note
from notesearch.state import State
from notesearch.writer import apply_pending

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(os.path.dirname(HERE), "sample")
READ, WRITE = "read-token-for-tests", "write-token-for-tests"
HASHES = {"read": {"sha256": hashlib.sha256(READ.encode()).hexdigest(), "scope": "read"},
          "write": {"sha256": hashlib.sha256(WRITE.encode()).hexdigest(), "scope": "write"}}
R = {"Authorization": f"Bearer {READ}"}
W = {"Authorization": f"Bearer {WRITE}"}


def sample_files():
    out = []
    for root, _, names in os.walk(SAMPLE):
        for n in sorted(names):
            full = os.path.join(root, n)
            rel = os.path.relpath(full, SAMPLE).replace(os.sep, "/")
            data = open(full, "rb").read()
            out.append({"path": rel, "content_b64": base64.b64encode(data).decode(), "blob_sha": git_blob_sha(data)})
    return out


def make(tmp_path, **over):
    s = Settings(**over) if over else Settings()
    emb = FakeEmbedder()
    db = str(tmp_path / "index.sqlite")
    st = State(str(tmp_path / "state"), s)
    runs = []

    def open_index():
        return Index(db, emb, s)

    def trigger():
        ix = open_index()
        runs.append(apply_pending(ix, st))
        ix.close()

    client = TestClient(create_app(open_index, st, trigger, HASHES, s))
    return client, st, runs, open_index


# ---------------- unit ----------------
def test_chunking_and_frontmatter():
    fm, body = parse_note("---\ncontact_id: c-x\n---\n# A\nhello\n## B\n" + "y" * 2000)
    assert fm == {"contact_id": "c-x"}
    ch = chunk_text(body, 800, 150)
    assert ch[0] == ("A", "hello")
    assert all(len(t) <= 800 for _, t in ch) and len([c for c in ch if c[0] == "B"]) == 3


def test_contacts_refuses_missing_id_column():
    with pytest.raises(ct.ContactsError):
        ct.parse_csv("name,email\nA,a@example.com\n")
    with pytest.raises(ct.ContactsError):
        ct.parse_csv("contact_id,name\nc-a,A\nc-a,A again\n")


def test_redirects_both_forms_and_chains():
    rows = ct.parse_csv(open(os.path.join(SAMPLE, "people/contacts.csv")).read())
    by = {r["contact_id"]: r for r in rows}
    assert by["c-jane-d"]["merged_into"] == "c-jane-doe"           # merged_into column
    assert by["c-s-sample"]["merged_into"] == "c-sam-sample"       # "merged into <id>" cell
    chain = {"a": {"contact_id": "a", "merged_into": "b"}, "b": {"contact_id": "b", "merged_into": "c"},
             "c": {"contact_id": "c", "merged_into": ""}}
    assert ct.resolve("a", chain) == ("c", ["a", "b", "c"])
    loop = {"a": {"contact_id": "a", "merged_into": "b"}, "b": {"contact_id": "b", "merged_into": "a"}}
    assert ct.resolve("a", loop)[0] is None


def test_card_never_has_email_or_phone():
    row = {"contact_id": "c-a", "name": "A", "email": "a@example.com", "phone": "555", "organization": "X"}
    assert "example.com" not in ct.card_text(row) and "555" not in ct.card_text(row)
    assert "email" not in ct.public_card(row) and "phone" not in ct.public_card(row)


# ---------------- end to end (API + writer) ----------------
def test_end_to_end(tmp_path):
    c, st, runs, open_index = make(tmp_path)
    h = c.get("/v1/health").json()
    assert h["status"] == "up" and h["chunks"] == 0
    assert c.post("/v1/ask", json={"question": "x"}).status_code == 401
    assert c.post("/v1/ingest", headers=R, json={}).status_code == 403
    bad = sample_files()[:1]
    bad[0] = dict(bad[0], blob_sha="0" * 40)
    assert c.post("/v1/ingest", headers=W, json={"files": bad}).status_code == 400
    assert c.post("/v1/ingest", headers=W, json={"files": [
        {"path": "../etc/passwd", "content_b64": "", "blob_sha": git_blob_sha(b"")}]}).status_code == 400

    r = c.post("/v1/ingest", headers=W, json={"files": sample_files()}).json()
    assert r["status"] == "accepted_running" and len(runs) == 1
    h = c.get("/v1/health").json()
    assert h["notes"] == 11 and h["contacts_active"] == 6 and h["contacts_redirects"] == 2
    assert "Jane" not in json.dumps(h)  # health never shows note text

    res = c.post("/v1/ask", headers=R, json={"question": "pilot summary pricing finance team"}).json()["results"]
    top = res[0]
    assert top["path"] == "people/notes/c-jane-doe.md" and top["contact_id"] == "c-jane-doe"
    assert top["contact"]["organization"] == "Example Co" and "email" not in top["contact"]
    assert "jane@example.com" not in json.dumps(res)

    res = c.post("/v1/ask", headers=R, json={"question": "garlic tomatoes garden"}).json()["results"]
    assert res[0]["path"] == "notes/ideas.md"

    # contact lookup follows redirects (old id and old name)
    j = c.get("/v1/contact/c-jane-d", headers=R).json()
    assert j["contact_id"] == "c-jane-doe" and j["redirected"] and j["merged_from"] == ["c-jane-d"]
    assert j["excerpts"] and j["excerpts"][0]["path"] == "people/notes/c-jane-doe.md"
    assert "email" not in j["card"]
    j = c.get("/v1/contact/S. Sample", headers=R).json()
    assert j["contact_id"] == "c-sam-sample"
    assert c.get("/v1/contact/c-nobody", headers=R).status_code == 404

    s = c.get("/v1/ingest/status", headers=W).json()
    chk = s["contact_checks"]
    probs = json.dumps(chk["orphans"])
    assert "c-pat-placeholder" in probs and "c-unknown-person" in probs
    assert {"match_on": "email", "contact_ids": ["c-alex-example", "c-alex-example-2"]} in chk["likely_duplicates"]
    assert not any("c-jane-d" in d["contact_ids"] for d in chk["likely_duplicates"])  # redirects excluded
    assert chk["broken_redirects"] == []
    assert s["pending_requests"] == 0


def test_same_content_not_reembedded(tmp_path):
    c, st, runs, oi = make(tmp_path, min_gap_seconds=0)
    c.post("/v1/ingest", headers=W, json={"files": sample_files()})
    c.post("/v1/ingest", headers=W, json={"files": sample_files()})
    assert runs[1]["chunks_added"] == 0 and runs[1]["contacts"]["embedded"] == 0


def test_contacts_file_without_id_refused(tmp_path):
    c, *_ = make(tmp_path)
    data = b"name,email\nA,a@example.com\n"
    r = c.post("/v1/ingest", headers=W, json={"files": [
        {"path": "people/contacts.csv", "content_b64": base64.b64encode(data).decode(), "blob_sha": git_blob_sha(data)}]})
    assert r.status_code == 400 and "contact_id" in r.json()["detail"]


def test_gap_and_daily_cap(tmp_path):
    c, st, runs, oi = make(tmp_path, max_runs_per_day=2, min_gap_seconds=600)
    files = sample_files()
    assert c.post("/v1/ingest", headers=W, json={"files": files[:2]}).json()["status"] == "accepted_running"
    r = c.post("/v1/ingest", headers=W, json={"files": files[2:4]}).json()
    assert r["status"] == "waiting_for_gap" and 0 < r["retry_after_s"] <= 600 and len(runs) == 1
    # pretend the gap passed: the next request runs and picks up the waiting one too
    rr = st.runs(); rr[-1]["ts"] -= 601; st.write("runs.json", rr)
    assert c.post("/v1/ingest", headers=W, json={"files": files[4:6]}).json()["status"] == "accepted_running"
    assert runs[-1]["requests"] == 2
    rr = st.runs(); rr[-1]["ts"] -= 601; st.write("runs.json", rr)
    r = c.post("/v1/ingest", headers=W, json={"files": files[6:]}).json()
    assert r["status"] == "queued_for_scheduled_run" and len(runs) == 2
    assert c.get("/v1/health").json()["runs_today"] == 2
    # nightly job applies what was held back, nothing dropped
    ix = oi(); summary = apply_pending(ix, st, kind="nightly"); ix.close()
    assert summary["requests"] == 1
    assert c.get("/v1/health").json()["notes"] == 11


def test_rate_limit(tmp_path):
    c, *_ = make(tmp_path, max_requests_per_hour=3)
    codes = [c.post("/v1/ingest", headers=W, json={}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_request_size_and_file_count(tmp_path):
    c, *_ = make(tmp_path, max_request_bytes=500, max_files_per_request=2)
    assert c.post("/v1/ingest", headers=W, json={"files": sample_files()}).status_code == 413
    c, *_ = make(tmp_path / "b", max_files_per_request=2)
    assert c.post("/v1/ingest", headers=W, json={"files": sample_files()[:3]}).status_code == 400


def test_delete_brakes_and_release(tmp_path):
    c, st, runs, oi = make(tmp_path, min_gap_seconds=0, max_deletes_per_request=2, max_manifest_missing_pct=5)
    c.post("/v1/ingest", headers=W, json={"files": sample_files()})
    notes = ["notes/ideas.md", "notes/recipes.txt", "notes/2026-10-15-weekly-plan.md"]
    c.post("/v1/ingest", headers=W, json={"deletes": notes})
    s = c.get("/v1/ingest/status", headers=W).json()
    assert sorted(s["held"]["deletes"]) == sorted(notes) and s["counts"]["notes"] == 11
    c.post("/v1/ingest", headers=W, json={"release": ["deletes"]})
    assert c.get("/v1/health").json()["notes"] == 8
    # manifest: one missing of 8 = 12.5% > 5% -> held; stale manifest -> no deletes
    keep = sorted(oi().note_paths())
    c.post("/v1/ingest", headers=W, json={"manifest": keep[1:], "manifest_generated_at": time.time()})
    assert c.get("/v1/ingest/status", headers=W).json()["held"]["deletes"] == [keep[0]]
    c, st, runs, oi = make(tmp_path / "m", min_gap_seconds=0, max_manifest_missing_pct=50)
    c.post("/v1/ingest", headers=W, json={"files": sample_files()})
    keep = sorted(oi().note_paths())
    c.post("/v1/ingest", headers=W, json={"manifest": keep[1:], "manifest_generated_at": time.time() - 30 * 3600})
    assert c.get("/v1/health").json()["notes"] == 11
    c.post("/v1/ingest", headers=W, json={"manifest": keep[1:], "manifest_generated_at": time.time()})
    assert c.get("/v1/health").json()["notes"] == 10


def test_contacts_big_drop_is_held_and_nothing_deleted(tmp_path):
    c, st, runs, oi = make(tmp_path, min_gap_seconds=0, max_contact_drop_pct=20)
    header = ",".join(ct.COLUMNS)
    rows = [f"c-p{i},Person {i},Org,,person,,,,met,,,,people/notes/c-p{i}.md,," for i in range(12)]
    def send(lines):
        data = ("\n".join([header] + lines) + "\n").encode()
        return c.post("/v1/ingest", headers=W, json={"files": [{"path": "people/contacts.csv",
                      "content_b64": base64.b64encode(data).decode(), "blob_sha": git_blob_sha(data)}]})
    send(rows)
    assert c.get("/v1/health").json()["contacts_active"] == 12
    send(rows[:6])
    s = c.get("/v1/ingest/status", headers=W).json()
    assert s["held"]["contacts"] and s["counts"]["contacts_active"] == 12
    c.post("/v1/ingest", headers=W, json={"release": ["contacts"]})
    assert c.get("/v1/health").json()["contacts_active"] == 6
    ix = oi(); assert len(ix.contact_rows(include_gone=True)) == 12; ix.close()  # nothing deleted


def test_bad_token_and_constant_scope(tmp_path):
    c, *_ = make(tmp_path)
    assert c.post("/v1/ask", headers={"Authorization": "Bearer nope"}, json={"question": "x"}).status_code == 401
    assert c.post("/v1/ask", headers=W, json={"question": "x"}).status_code == 200  # write can read
    assert c.post("/v1/ask", headers=R, json={"question": "x" * 2001}).status_code == 400


# ---------------- scripts ----------------
def test_push_git_mode_and_folder_mode(tmp_path, monkeypatch):
    import push
    c, st, runs, oi = make(tmp_path, min_gap_seconds=0)

    def fake_post(url, token, body, path="/v1/ingest"):
        r = c.post(path, headers={"Authorization": f"Bearer {token}"}, json=body)
        return r.status_code, r.json()
    monkeypatch.setattr(push, "post", fake_post)
    tok = tmp_path / "w.token"; tok.write_text(WRITE)
    repo = tmp_path / "repo"; shutil.copytree(SAMPLE, repo)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q"); g("-c", "user.name=t", "-c", "user.email=t@example.com", "add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one")
    sf = str(tmp_path / "ps.json")
    args = ["--source", str(repo), "--url", "http://x", "--token-file", str(tok), "--state-file", sf]
    assert push.main(args + ["--dry-run"]) == 0 and runs == []
    assert push.main(args) == 0
    assert c.get("/v1/health").json()["notes"] == 11
    (repo / "notes/recipes.txt").unlink()
    (repo / "notes/new.md").write_text("# New\nA note about sailing lessons.\n")
    (repo / "notes/uncommitted.md").write_text("# Not committed\n")
    g("add", "notes/new.md", "notes/recipes.txt")
    g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "two")
    assert push.main(args) == 0
    paths = oi().note_paths()
    assert "notes/new.md" in paths and "notes/recipes.txt" not in paths and "notes/uncommitted.md" not in paths
    assert runs[-1]["requests"] == 1 and runs[-1]["notes_indexed"] == 1

    # folder mode
    c2, st2, runs2, oi2 = make(tmp_path / "f", min_gap_seconds=0)
    monkeypatch.setattr(push, "post", lambda url, token, body, path="/v1/ingest": (
        lambda r: (r.status_code, r.json()))(c2.post(path, headers={"Authorization": f"Bearer {token}"}, json=body)))
    folder = tmp_path / "folder"; shutil.copytree(SAMPLE, folder)
    fargs = ["--source", str(folder), "--url", "http://x", "--token-file", str(tok),
             "--state-file", str(tmp_path / "fs.json"), "--include", "notes"]
    assert push.main(fargs) == 0
    assert oi2().note_paths() == {"notes/ideas.md", "notes/recipes.txt", "notes/2026-10-15-weekly-plan.md"}
    assert oi2().counts()["contacts_active"] == 6  # contacts file always goes


def test_push_skips_plm_folder_unless_included(tmp_path):
    import push
    brain = tmp_path / "brain"
    shutil.copytree(SAMPLE, brain)
    (brain / "plm" / "sample" / "notes").mkdir(parents=True)
    (brain / "plm" / "README.md").write_text("# Starter readme\n")
    (brain / "plm" / "sample" / "notes" / "fake.md").write_text("# Fake\n")
    (brain / "plmnotes").mkdir()
    (brain / "plmnotes" / "keep.md").write_text("# Not the plm folder\n")
    for mode in ("folder", "git"):
        if mode == "git":
            g = lambda *a: subprocess.run(["git", "-C", str(brain), *a], check=True, capture_output=True)
            g("init", "-q"); g("add", ".")
            g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one")
            files = push.changes_git(str(brain), {}, [], "people/contacts.csv", True)[1]
            files_inc = push.changes_git(str(brain), {}, ["plm"], "people/contacts.csv", True)[1]
        else:
            files = push.changes_folder(str(brain), {}, [], "people/contacts.csv", True)[1]
            files_inc = push.changes_folder(str(brain), {}, ["plm/"], "people/contacts.csv", True)[1]
        # default: plm/ skipped, a folder that only starts with "plm" is kept
        assert not any(p.startswith("plm/") for p in files), mode
        assert "plmnotes/keep.md" in files and "notes/ideas.md" in files, mode
        # --include naming plm/ still picks it up (plus the contacts file, which always goes)
        assert set(files_inc) == {"plm/README.md", "plm/sample/notes/fake.md", "people/contacts.csv"}, mode


def test_admin_tokens_saved_private_and_never_printed(tmp_path, capsys):
    import admin
    hashes = admin.make_tokens("demo-app", dry_run=False, config_root=str(tmp_path))
    out = capsys.readouterr().out
    for name in ("read", "write"):
        p = tmp_path / "demo-app" / f"{name}.token"
        tok = p.read_text().strip()
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert tok not in out and hashes[name]["sha256"] == hashlib.sha256(tok.encode()).hexdigest()
