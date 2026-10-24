"""Small JSON state kept next to the index: pending ingest requests, held
deletes, run history (for the daily cap and gap) and request counts (for the
hourly rate limit). Plain files so you can read them yourself."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings


class State:
    def __init__(self, root: str, settings: Settings | None = None):
        self.root = root
        self.s = settings or Settings()
        os.makedirs(os.path.join(root, "pending"), exist_ok=True)

    # ---- json helpers ----
    def _p(self, name):
        return os.path.join(self.root, name)

    def read(self, name, default):
        try:
            with open(self._p(name)) as f:
                return json.load(f)
        except FileNotFoundError:
            return default

    def write(self, name, data):
        tmp = self._p(name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self._p(name))

    # ---- day / caps ----
    def day_of(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, ZoneInfo(self.s.timezone)).strftime("%Y-%m-%d")

    def runs(self) -> list[dict]:
        return self.read("runs.json", [])

    def runs_today(self, now: float | None = None) -> int:
        now = now or time.time()
        today = self.day_of(now)
        return sum(1 for r in self.runs() if r.get("counted", True) and self.day_of(r["ts"]) == today)

    def run_gate(self, now: float | None = None) -> tuple[bool, str, int]:
        """(may_run_now, reason, retry_after_seconds)."""
        now = now or time.time()
        if self.runs_today(now) >= self.s.max_runs_per_day:
            return False, "daily_cap", 0
        counted = [r for r in self.runs() if r.get("counted", True)]
        if counted:
            wait = int(counted[-1]["ts"] + self.s.min_gap_seconds - now)
            if wait > 0:
                return False, "min_gap", wait
        return True, "ok", 0

    def record_run(self, kind: str, summary: dict, counted: bool = True, now: float | None = None):
        runs = self.runs()
        runs.append({"ts": now or time.time(), "kind": kind, "counted": counted, "summary": summary})
        self.write("runs.json", runs[-500:])

    def rate_ok(self, token_name: str, now: float | None = None) -> bool:
        now = now or time.time()
        rate = self.read("rate.json", {})
        recent = [t for t in rate.get(token_name, []) if t > now - 3600]
        ok = len(recent) < self.s.max_requests_per_hour
        if ok:
            recent.append(now)
        rate[token_name] = recent
        self.write("rate.json", rate)
        return ok

    # ---- pending queue ----
    def add_pending(self, payload: dict) -> str:
        rid = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        payload = dict(payload, request_id=rid)
        with open(os.path.join(self.root, "pending", rid + ".json"), "w") as f:
            json.dump(payload, f)
        return rid

    def pending_files(self) -> list[str]:
        d = os.path.join(self.root, "pending")
        return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json"))

    def held(self) -> dict:
        return self.read("held.json", {"deletes": [], "contacts": None, "reasons": []})
