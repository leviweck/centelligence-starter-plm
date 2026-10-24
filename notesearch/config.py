"""All tunable settings in one place. Every value can be overridden with an
environment variable of the same name (prefix NS_)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # Run caps (rules in your own code, not Modal limits)
    max_runs_per_day: int = field(default_factory=lambda: _int("NS_MAX_RUNS_PER_DAY", 24))
    min_gap_seconds: int = field(default_factory=lambda: _int("NS_MIN_GAP_SECONDS", 600))
    max_requests_per_hour: int = field(default_factory=lambda: _int("NS_MAX_REQUESTS_PER_HOUR", 120))
    max_request_bytes: int = field(default_factory=lambda: _int("NS_MAX_REQUEST_BYTES", 1_000_000))
    max_files_per_request: int = field(default_factory=lambda: _int("NS_MAX_FILES_PER_REQUEST", 200))
    # Delete brakes
    max_deletes_per_request: int = field(default_factory=lambda: _int("NS_MAX_DELETES_PER_REQUEST", 50))
    max_manifest_missing_pct: int = field(default_factory=lambda: _int("NS_MAX_MANIFEST_MISSING_PCT", 5))
    max_contact_drop_pct: int = field(default_factory=lambda: _int("NS_MAX_CONTACT_DROP_PCT", 20))
    # Day boundary for the daily cap (IANA name, e.g. America/Chicago)
    timezone: str = field(default_factory=lambda: os.environ.get("NS_TIMEZONE", "UTC"))
    # Chunking and search
    chunk_chars: int = field(default_factory=lambda: _int("NS_CHUNK_CHARS", 800))
    chunk_overlap: int = field(default_factory=lambda: _int("NS_CHUNK_OVERLAP", 150))
    top_k: int = field(default_factory=lambda: _int("NS_TOP_K", 6))
    max_question_chars: int = field(default_factory=lambda: _int("NS_MAX_QUESTION_CHARS", 2000))
    # Which files are indexed
    note_suffixes: tuple = (".md", ".txt")
    contacts_path: str = field(default_factory=lambda: os.environ.get("NS_CONTACTS_PATH", "people/contacts.csv"))
