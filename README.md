# Log Analyser

A small web app for uploading `.log`/`.txt` files, parsing them into structured
entries, filtering by level / time range / keyword, and viewing metrics with
charts. Built with Python + Flask, SQLite, and a single-page UI using Chart.js.

## Run it

```bash
docker compose up --build
```

Then open http://localhost:5000 — upload a log file and start exploring.

Data persists in a named Docker volume (`logdata`, mounted at `/data`), so
uploads survive `docker compose down` / `up`.

## Test it

Inside the project's virtualenv (`.venv`):

```powershell
python -m pytest -q
```

Tests cover the parser (`tests/test_parser.py`), the database layer
(`tests/test_db.py`), and the HTTP API (`tests/test_api.py`).
`tests/sample.log` contains all supported formats plus stack traces,
continuation lines, and malformed lines.

## Architecture

```
parser.py  ->  db.py  ->  app.py  ->  templates/index.html
(parse)       (SQLite)   (Flask API)   (Chart.js UI)
```

- **parser.py** — streaming parser. Turns raw lines into
  `(timestamp, level, message)` tuples; no filtering.
- **db.py** — SQLite access layer. All filtering (level, time range,
  keyword `LIKE`) happens here in parameterized SQL.
- **app.py** — Flask routes: upload, log queries, metrics, file management.
- **index.html** — single page: upload form, filters, metric cards,
  Chart.js charts, paginated log table.

## Supported log formats

| Format | Example |
|---|---|
| ISO date + level | `2026-11-18 14:32:01 INFO server started` |
| Bracketed level + ISO | `[ERROR] 2026-11-18T14:35:40Z upstream timeout` |
| Syslog | `Nov 18 14:40:01 web01 nginx[812]: INFO GET / 200` |

Levels are normalized (`WARNING`→`WARN`, `CRITICAL`→`FATAL`).

## Continuation & skipped lines

Stack traces and similar multi-line blocks are appended to the previous
entry's message when a non-matching line:

- starts with whitespace, or
- starts with `Traceback` / `Caused by:`, or
- looks like `SomethingError:` / `SomethingException`.

Any other non-matching line is counted as **skipped** and breaks the
current entry (following indented lines become orphans, also skipped).
Blank lines are ignored. Upload responses report `parsed` / `skipped`.

## Assumptions & limits

- Timestamps **without a timezone are assumed UTC**; all stored timestamps
  are normalized to UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`).
- Syslog timestamps have **no year — the current year is used**.
- Uploads are capped at **10 MB** and limited to `.log` / `.txt`.
- SQLite on a single node — fine for interview-sized data, not for
  high-concurrency production use. WAL mode + foreign keys are enabled
  on every connection; deleting a file cascades to its entries.
- The UI loads Chart.js from a CDN, so it needs internet access on first
  page load.

## API summary

| Endpoint | Purpose |
|---|---|
| `POST /api/upload` | multipart `file` field → `{file_id, filename, parsed, skipped}` |
| `GET /api/logs` | `file_id`, `level` (repeatable), `start`, `end`, `q`, `limit`, `offset` → `{total, rows}` |
| `GET /api/metrics` | same filters → totals, per-level counts, time series, top repeated errors |
| `GET /api/files` / `DELETE /api/files/<id>` | list / delete uploaded files |

All `start`/`end` values are UTC ISO-8601 strings.

## Project layout

```
app.py  parser.py  db.py  gunicorn.conf.py
Dockerfile  docker-compose.yml  requirements.txt
templates/index.html
tests/{test_parser,test_db,test_api}.py  tests/sample.log
```

Run locally without Docker (PowerShell):

```powershell
$env:DB_PATH="./data/logs.db"; python app.py
```

(gunicorn is only used inside the Docker image — it doesn't run on Windows.)
