"""Modal app: your notes search service.

Deploy:   modal deploy app.py
Settings: environment variables (see notesearch/config.py and README).
  NS_APP_NAME     app name in your Modal workspace (default: notes-search)
  NS_TIMEZONE     your time zone for the daily cap and the nightly job
  NS_SECRET_NAME  Modal Secret holding NS_TOKEN_HASHES (default: notes-search-tokens)
"""
from __future__ import annotations

import json
import os

import modal

APP_NAME = os.environ.get("NS_APP_NAME", "notes-search")
TZ = os.environ.get("NS_TIMEZONE", "UTC")
SECRET_NAME = os.environ.get("NS_SECRET_NAME", f"{APP_NAME}-tokens")
EMBED_MODEL = os.environ.get("NS_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
NIGHTLY = os.environ.get("NS_NIGHTLY_CRON", "45 0 * * *")
MODEL_DIR = "/models"
INDEX_DIR, STATE_DIR = "/index", "/state"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]==0.115.*", "numpy<3", "fastembed==0.9.*")
    .env({"NS_TIMEZONE": TZ, "NS_EMBED_MODEL": EMBED_MODEL})
    .run_commands(  # bake the model into the image so cold starts don't download it
        f"python -c \"from fastembed import TextEmbedding; TextEmbedding('{EMBED_MODEL}', cache_dir='{MODEL_DIR}')\""
    )
    .add_local_python_source("notesearch")
)
app = modal.App(APP_NAME, image=image)
index_vol = modal.Volume.from_name(f"{APP_NAME}-index", create_if_missing=True)
state_vol = modal.Volume.from_name(f"{APP_NAME}-state", create_if_missing=True)
secret = modal.Secret.from_name(SECRET_NAME)


_EMB = None


def _open_index(readonly: bool = False):
    from notesearch.embed import LocalEmbedder
    from notesearch.index import Index

    global _EMB
    if _EMB is None:
        _EMB = LocalEmbedder(EMBED_MODEL, cache_dir=MODEL_DIR)
    return Index(f"{INDEX_DIR}/index.sqlite", _EMB, readonly=readonly)


def _run_writer(kind: str) -> dict:
    from notesearch.state import State
    from notesearch.writer import apply_pending

    index_vol.reload()
    state_vol.reload()
    ix = _open_index()
    try:
        summary = apply_pending(ix, State(STATE_DIR), kind=kind)
    finally:
        ix.close()
    index_vol.commit()
    print(json.dumps({k: v for k, v in summary.items() if k != "contacts"}))
    return summary


@app.function(cpu=2, memory=2048, timeout=1800, max_containers=1,
              volumes={INDEX_DIR: index_vol, STATE_DIR: state_vol})
def writer():
    return _run_writer("run")


@app.function(cpu=2, memory=2048, timeout=3600, max_containers=1,
              volumes={INDEX_DIR: index_vol, STATE_DIR: state_vol},
              schedule=modal.Cron(NIGHTLY, timezone=TZ))
def nightly():
    """Applies anything held back by the caps."""
    return _run_writer("nightly")


@app.function(cpu=1, memory=2048, max_containers=1, scaledown_window=300, secrets=[secret],
              volumes={INDEX_DIR: index_vol, STATE_DIR: state_vol})
@modal.asgi_app()
def api():
    from notesearch.api import create_app
    from notesearch.state import State

    hashes = json.loads(os.environ.get("NS_TOKEN_HASHES", "{}"))

    def open_index():
        try:
            index_vol.reload()
        except Exception:  # reload refuses while a file is open; stale-by-seconds is fine
            pass
        return _open_index(readonly=True)

    return create_app(open_index, State(STATE_DIR), lambda: writer.spawn(), hashes,
                      after_state_write=state_vol.commit)
