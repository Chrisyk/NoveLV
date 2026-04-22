# Novelv

Flask app for analyzing Japanese text against an Anki vocabulary cache, with Yomitan integration.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_app.py
```

Open http://localhost:5000 (or set `PORT`).

## Configuration (environment variables)

| Variable | Purpose | Default |
|----------|---------|---------|
| `FLASK_SECRET_KEY` | Session signing key | Random (non-debug) or `dev` when `FLASK_DEBUG` is set |
| `FLASK_DEBUG` | Enable Flask debug mode when running `run_app.py` | unset |
| `PORT` | HTTP port for `run_app.py` | `5000` |
| `YOMITAN_API_URL` | Yomitan API base URL | `http://127.0.0.1:19633` |
| `YOMITAN_API_TIMEOUT` | Request timeout (seconds) | `100` |
| `YOMITAN_CHUNK_SIZE` | Max characters per tokenize chunk | `300` |
| `ANKI_CONNECT_URL` | AnkiConnect URL | `http://127.0.0.1:8765` |

Optional: place `instance/config.py` next to the app (Flask loads it silently) for extra `app.config` keys.

## Data directories

- `novels/` — uploaded novels
- `data/` — vocabulary caches, `ignored_words.json`, `scan_history.db`
