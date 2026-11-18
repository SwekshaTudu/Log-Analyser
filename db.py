"""SQLite access layer — all filtering happens here in SQL.

DB location comes from the DB_PATH env var (default /data/logs.db).
Every connection enables PRAGMA foreign_keys=ON (so ON DELETE CASCADE
works) and PRAGMA journal_mode=WAL, and returns sqlite3.Row objects.
"""

import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_LIMIT = 500
MAX_LIMIT = 1000
BATCH_SIZE = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_entries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id   INTEGER NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_file
    ON log_entries(file_id, timestamp, level);
"""


def _db_path():
    return os.environ.get("DB_PATH", "/data/logs.db")


def _connect():
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _utc_now_iso():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def init_db():
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_file(filename):
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO log_files (filename, uploaded_at) VALUES (?, ?)",
                (filename, _utc_now_iso()),
            )
            return cur.lastrowid
    finally:
        conn.close()


def insert_entries(file_id, events):
    """Consume ("entry", ...)/("stats", ...) events from parser.iter_parse.

    Inserts entries in batches of BATCH_SIZE inside one transaction.
    Returns the stats dict ({"entries": n, "skipped": n}).
    """
    stats = {"entries": 0, "skipped": 0}
    batch = []
    conn = _connect()
    try:
        with conn:
            for kind, payload in events:
                if kind == "entry":
                    ts, level, message = payload
                    batch.append((file_id, ts, level, message))
                    if len(batch) >= BATCH_SIZE:
                        _flush(conn, batch)
                        batch.clear()
                elif kind == "stats":
                    stats.update(payload)
            if batch:
                _flush(conn, batch)
    finally:
        conn.close()
    return stats


def _flush(conn, batch):
    conn.executemany(
        "INSERT INTO log_entries (file_id, timestamp, level, message)"
        " VALUES (?, ?, ?, ?)",
        batch,
    )


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(file_id, level, start, end, q):
    """Build (WHERE clause, params) shared by all filtered queries."""
    clauses, params = [], []
    if file_id is not None:
        clauses.append("file_id = ?")
        params.append(file_id)
    if level:
        levels = [level] if isinstance(level, str) else list(level)
        levels = [str(lv).upper() for lv in levels if str(lv).strip()]
        if levels:
            marks = ",".join("?" for _ in levels)
            clauses.append(f"level IN ({marks})")
            params.extend(levels)
    if start:
        clauses.append("timestamp >= ?")
        params.append(start)
    if end:
        clauses.append("timestamp <= ?")
        params.append(end)
    if q:
        clauses.append("message LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(q)}%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def _clamp_limit(limit):
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def query_entries(file_id=None, level=None, start=None, end=None, q=None,
                  limit=DEFAULT_LIMIT, offset=0):
    """Return {"total": n, "rows": [...]} with all filters applied in SQL."""
    where, params = _where(file_id, level, start, end, q)
    try:
        off = max(0, int(offset))
    except (TypeError, ValueError):
        off = 0
    lim = _clamp_limit(limit)

    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM log_entries{where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT id, file_id, timestamp, level, message"
            f" FROM log_entries{where}"
            " ORDER BY timestamp, id LIMIT ? OFFSET ?",
            params + [lim, off],
        ).fetchall()
        return {"total": total, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def query_metrics(file_id=None, level=None, start=None, end=None, q=None):
    """Return {total, by_level, series, top_messages} for the filters.

    series buckets are substr(timestamp,1,16) (minutes) when the filtered
    time span is <= 6 hours, otherwise substr(timestamp,1,13) (hours).
    top_messages lists the 5 most frequent ERROR/FATAL first lines.
    """
    where, params = _where(file_id, level, start, end, q)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM log_entries{where}", params
        ).fetchone()["c"]

        by_level = {
            r["level"]: r["c"]
            for r in conn.execute(
                f"SELECT level, COUNT(*) AS c FROM log_entries{where}"
                " GROUP BY level ORDER BY level",
                params,
            )
        }

        series = []
        span_row = conn.execute(
            f"SELECT MIN(timestamp) AS mn, MAX(timestamp) AS mx"
            f" FROM log_entries{where}",
            params,
        ).fetchone()
        if span_row["mn"] is not None:
            mn = datetime.fromisoformat(span_row["mn"].replace("Z", "+00:00"))
            mx = datetime.fromisoformat(span_row["mx"].replace("Z", "+00:00"))
            width = 16 if (mx - mn).total_seconds() <= 6 * 3600 else 13
            series = [
                dict(r)
                for r in conn.execute(
                    f"SELECT substr(timestamp, 1, {width}) AS bucket,"
                    " COUNT(*) AS count,"
                    " SUM(CASE WHEN level IN ('ERROR','FATAL')"
                    "     THEN 1 ELSE 0 END) AS errors"
                    f" FROM log_entries{where}"
                    " GROUP BY bucket ORDER BY bucket",
                    params,
                )
            ]

        err_where = (
            where + " AND level IN ('ERROR','FATAL')"
            if where
            else " WHERE level IN ('ERROR','FATAL')"
        )
        err_params = params
        top_messages = [
            dict(r)
            for r in conn.execute(
                "SELECT first_line, COUNT(*) AS count FROM ("
                "  SELECT CASE WHEN instr(message, char(10)) > 0"
                "      THEN substr(message, 1, instr(message, char(10)) - 1)"
                "      ELSE message END AS first_line"
                f"  FROM log_entries{err_where}"
                ") GROUP BY first_line"
                " ORDER BY count DESC, first_line LIMIT 5",
                err_params,
            )
        ]

        return {
            "total": total,
            "by_level": by_level,
            "series": series,
            "top_messages": top_messages,
        }
    finally:
        conn.close()


def list_files():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT f.id, f.filename, f.uploaded_at,"
            " COUNT(e.id) AS entry_count"
            " FROM log_files f"
            " LEFT JOIN log_entries e ON e.file_id = f.id"
            " GROUP BY f.id ORDER BY f.id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_file(file_id):
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM log_files WHERE id = ?", (file_id,)
            )
            return cur.rowcount > 0
    finally:
        conn.close()
