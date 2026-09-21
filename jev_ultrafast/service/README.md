# Jev HTTP service (T-1)

Wraps `jev_ultrafast.Agent` in a small FastAPI service so Hades / NyxStrike can
drive a browser subgoal over HTTP. The agent loop in `agent.py` is **untouched** —
this module only calls it.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/run_goal` | `{url, goal, session_id}` | `{run_id, status, session_id, elapsed_ms, error}` |
| POST | `/extract_surface` | `{url, session_id}` | one indexed snapshot: `{run_id:null, session_id, status, url, title, text, elements}` |
| GET | `/get_evidence/{run_id}` | — | full stored run: history + final snapshot + budget fields |

- `run_goal` runs the agent loop to `done` / `blocked` (or the budget). It stores
  `history` + the final `page` snapshot under a generated `run_id` (`jev-<uuid>`).
- `extract_surface` takes **one** observation and returns the indexed element table
  without ever starting the loop — cheap recon (1 TypeSafe call, or 0).
- `session_id` is carried through the contract now; persistent browser context
  between `run_goal` calls is a later task (T-4). Here it is a plumbed field only.

## Running locally

The service needs the same prerequisites the demo does: a dedicated Chrome with
remote debugging plus the `TYPESAFE_API_KEY` / `TEXT_MODEL_API_KEY` from `.env`.

```bash
# from the repo root
uv run jev-service            # starts on 127.0.0.1:8765
# or a custom port
JUV_SERVICE_PORT=9000 uv run jev-service
```

Then:

```bash
curl -s -X POST http://127.0.0.1:8765/run_goal \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","goal":"Search for a book","session_id":"camp-1"}'

curl -s -X POST http://127.0.0.1:8765/extract_surface \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","session_id":"camp-1"}'

curl -s http://127.0.0.1:8765/get_evidence/<run_id>
```

Without Chrome running, `run_goal` / `extract_surface` return `503` with a
`Browser unavailable: ...` detail; `get_evidence` for an unknown id returns `404`.

## Tests

Tests are fully offline: the `Agent` is faked and the TypeSafe / text-model
`post_json` is patched to raise if ever called, per the repo rule that tests must
not hit paid APIs.

```bash
uv run pytest
```
