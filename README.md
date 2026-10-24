# Starter notes search service (PLM starter)

Your own small search service for your notes and contacts, running in **your own** Modal account.
It reads your notes (`.md`, `.txt`) and a contacts table (`people/contacts.csv`), turns them into
searchable data with a small embedding model that runs **inside your own container** (no outside
AI service), and answers questions with short excerpts plus the file each came from.

Nothing in this template points at anyone else's account. You bring your own Modal account,
your own notes, and your own tokens.

## What's inside

| Path | What it is |
|---|---|
| `app.py` | The Modal app: web API, a single Writer, and one nightly job |
| `notesearch/` | The code: chunking, index (SQLite + keyword index), search, contacts, run caps |
| `scripts/push.py` | Runs on your computer: sends changed notes and the contacts file |
| `scripts/admin.py` | Makes your read and write tokens; uploads only their fingerprints |
| `scripts/local_check.py` | Runs everything on your own computer with the real model, no cost |
| `sample/` | Fake notes and fake contacts to try it with |
| `tests/` | Offline tests (fake embeddings, no network) |

## How it works

- **Search** mixes meaning (embeddings, default `nomic-ai/nomic-embed-text-v1.5` on CPU) and
  keywords (SQLite FTS5), combined with reciprocal rank fusion. Notes are split on headings,
  then into 800-character pieces with 150 characters of overlap.
- **Index** is one SQLite file on a Modal Volume. Only the Writer changes it.
- **Tokens:** callers send `Authorization: Bearer <token>`. The server keeps only a SHA-256
  fingerprint of each token in a Modal Secret, never the token. A read token can search; a write
  token can also send notes.
- **Contacts:** `people/contacts.csv` (columns below). Each person's note starts with
  ```
  ---
  contact_id: c-jane-doe
  ---
  ```
  The service joins the two: excerpts from a person's note carry their contact card, and each
  active contact is searchable on its own (name, organization, role, type, stage, reach_by).
  **Email and phone are never embedded and never returned by search.**
- **Merges keep everything:** a row whose `merged_into` holds another contact_id (or any cell
  that reads `merged into c-…`) is a redirect. Old IDs and old names lead to the survivor.
  Chains resolve; loops and unknown targets are reported as broken. Nothing is ever deleted.
- **Checks** (shown in `/v1/ingest/status`, never fixed silently): rows without a note, notes
  naming an unknown contact_id, likely duplicates (same email, same phone, or same name +
  organization), and broken redirects.

### Contacts columns

`contact_id, name, organization, role, type, reach_by, email, phone, stage, identity,
last_confirmed, source, note_path, updated_at, merged_into`

Only `contact_id` is required by the service. IDs never change and are never reused
(`c-firstname-lastname`, `o-organization`, add `-2` for a second person with the same name).

### API

| Route | Token | What it does |
|---|---|---|
| `GET /v1/health` | none | Up, counts, runs used today, last run times. Never note text. |
| `POST /v1/ask` `{"question": "...", "top_k": 6}` | read | Top excerpts with file, heading, contact card |
| `GET /v1/contact/<contact_id or name>` | read | Contact card (no email/phone), merged-from IDs, top note excerpts. Follows redirects. |
| `POST /v1/ingest` | write | Files (base64 + git blob id, re-checked by the server), deletes, optional manifest, `release` of held items |
| `GET /v1/ingest/status` | write | Pending, held items, runs today, contact checks, last runs |

### Run caps (all settings in `notesearch/config.py`, change with `NS_*` environment variables)

| Setting | Default | Over the cap |
|---|---|---|
| Writer runs a day (your time zone, midnight to midnight) | 24 | Accepted and queued; the nightly job applies it. Nothing dropped. |
| Gap between runs | 10 minutes | Accepted and waits; `push.py` asks for a run when the gap is over |
| Ingest requests an hour, per token | 120 | `429 rate_limited`; `push.py` retries |
| Request size / files per request | 1 MB / 200 | Refused; `push.py` sends smaller batches |
| Deletes in one request | 50 | Held until you release them |
| Notes missing from a manifest | 5% | Held until you release them; a manifest older than 26 hours deletes nothing |
| Contacts rows dropping out in one file | 20% | Held until you release it |

These are rules in this code, not Modal limits.

## Setup, step by step

You need: Python 3.10+ and git on your computer, and a Modal account with a payment method and a
budget set (Modal: Settings → Usage & Billing).

1. **Get the code.** Copy this template into your own **private** repository, then clone it to
   your computer.
2. **Install Modal and sign in.**
   ```
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   modal token new          # opens a sign-in link; approve it in your browser
   modal profile list       # shows the profile and workspace you are signed in to
   ```
3. **Run the offline tests** (no cost):
   ```
   pip install -r requirements-dev.txt
   pytest -q
   ```
   Optional, still no cost: `pip install fastembed && python scripts/local_check.py`
4. **Pick your app name and time zone.**
   ```
   export NS_APP_NAME=notes-search            # any short name
   export NS_TIMEZONE=America/New_York        # your IANA time zone
   ```
5. **Make your tokens.** `python scripts/admin.py make-tokens --app $NS_APP_NAME`
   Tokens are saved in `~/.config/$NS_APP_NAME/read.token` and `write.token` (permission 600).
   Only their fingerprints go to Modal. Never paste a token into chat, notes or commits.
6. **Deploy.** `modal deploy app.py`
   The first deploy builds the image and downloads the embedding model into it, so it takes a
   few minutes. Copy the web address that ends in `.modal.run`.
7. **Check it's up.** `curl https://<your-address>.modal.run/v1/health`
8. **Load your notes.** First a dry run, then for real:
   ```
   python scripts/push.py --source ~/my-notes --url https://<your-address>.modal.run \
     --token-file ~/.config/$NS_APP_NAME/write.token --dry-run
   python scripts/push.py --source ~/my-notes --url https://<your-address>.modal.run \
     --token-file ~/.config/$NS_APP_NAME/write.token
   ```
   `--source` can be a git repository (sends what changed since the last send, read from the
   last commit) or a plain folder. A folder named `plm/` at the top of the source (where this starter sits inside your notes
   repository) is skipped by default, so its README and fake samples are never sent as notes.
   `--include notes,people` limits which folders go, and overrides that skip. The
   contacts file (`--contacts`, default `people/contacts.csv`) always goes when it changed.
9. **Ask a question.**
   ```
   curl -s -X POST https://<your-address>.modal.run/v1/ask \
     -H "Authorization: Bearer $(cat ~/.config/$NS_APP_NAME/read.token)" \
     -H "Content-Type: application/json" -d '{"question": "what did I promise this week?"}'
   ```
10. **Keep it current.** Run step 8 (without `--dry-run`) after each commit, for example from a
    git `post-commit` hook. Once a week add `--manifest` so notes you deleted leave the index.

### Held items

If `/v1/ingest/status` shows held deletes or a held contacts file, look at the list first. To
apply them: `python scripts/push.py ... --release deletes` (or `--release contacts`).

### Pause or stop

`modal app stop $NS_APP_NAME` stops the app and its nightly job. Your notes stay where they are;
the index can be rebuilt any time with `push.py --full`.

### Costs

The API container shuts down after 5 idle minutes. You pay Modal for the minutes your containers
run and for storage. Keep a budget set in Modal. Real costs depend on how many notes you have and
how often you send them.

## License

MIT. See [LICENSE](LICENSE).
