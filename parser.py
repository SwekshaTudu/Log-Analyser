"""Parse log lines into normalized entries. Parsing only — no filtering.

Supported line formats:
  A: "YYYY-MM-DD HH:MM:SS LEVEL message"          (naive -> assumed UTC)
  B: "[LEVEL] ISO8601 message"                    (offsets converted to UTC)
  C: "Mon DD HH:MM:SS host app[pid]: LEVEL msg"   (syslog, no year -> current year)

iter_parse(lines) is a streaming generator. It yields, in file order:
  ("entry", (timestamp_iso_utc, level, message))
followed by a final:
  ("stats", {"entries": int, "skipped": int})

Continuation rule: a non-matching line is appended to the previous
entry's message only if it (a) starts with whitespace, (b) starts with
"Traceback" or "Caused by:", or (c) matches ^[\\w.]+(Error|Exception)\\b.
Any other non-matching line increments `skipped` and resets the
previous entry, so following indented lines count as orphan
continuations (skipped) rather than attaching to earlier entries.
Blank lines are ignored.
"""

import re
from datetime import datetime, timezone

LEVEL_MAP = {"WARNING": "WARN", "CRITICAL": "FATAL"}
VALID_LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}

# (regex, timestamp parser). First match wins.
_FORMATS = [
    (
        re.compile(
            r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
            r"\s+(?P<level>[A-Za-z]+)\s+(?P<msg>.*)$"
        ),
        lambda ts: datetime.fromisoformat(ts),
    ),
    (
        re.compile(
            r"^\[(?P<level>[A-Za-z]+)\]\s+"
            r"(?P<ts>\d{4}-\d{2}-\d{2}T\S+)\s+(?P<msg>.*)$"
        ),
        lambda ts: datetime.fromisoformat(ts),
    ),
    (
        re.compile(
            r"^(?P<ts>[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
            r"\s+\S+\s+\S+?:\s+(?P<level>[A-Za-z]+)\s+(?P<msg>.*)$"
        ),
        lambda ts: datetime.strptime(
            "2000 " + " ".join(ts.split()), "%Y %b %d %H:%M:%S"
        ).replace(year=datetime.now().year),
    ),
]

_CONT_RE = re.compile(r"^[\w.]+(?:Error|Exception)\b")
_CONT_PREFIXES = ("Traceback", "Caused by:")


def _norm_level(raw):
    lvl = raw.strip().upper()
    return LEVEL_MAP.get(lvl, lvl)


def _is_continuation(line):
    if line[:1].isspace():
        return True
    if line.startswith(_CONT_PREFIXES):
        return True
    return bool(_CONT_RE.match(line))


def _to_utc_iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _match(line):
    """Return (timestamp_iso, level, message) or None."""
    for regex, ts_parser in _FORMATS:
        m = regex.match(line)
        if not m:
            continue
        level = _norm_level(m.group("level"))
        if level not in VALID_LEVELS:
            return None
        try:
            dt = ts_parser(m.group("ts"))
        except (ValueError, OverflowError):
            return None
        return (_to_utc_iso(dt), level, m.group("msg").rstrip())
    return None


def iter_parse(lines):
    """Stream-parse an iterable of lines.

    Yields ("entry", (ts_iso, level, message)) events, then a final
    ("stats", {"entries": n, "skipped": n}) event.
    """
    parsed = 0
    skipped = 0
    cur = None  # [ts_iso, level, message]

    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        m = _match(line)
        if m is not None:
            if cur is not None:
                parsed += 1
                yield ("entry", (cur[0], cur[1], cur[2]))
            cur = [m[0], m[1], m[2]]
        elif cur is not None and _is_continuation(line):
            cur[2] += "\n" + line
        else:
            if cur is not None:
                parsed += 1
                yield ("entry", (cur[0], cur[1], cur[2]))
                cur = None  # garbage breaks the entry; following indents are orphans
            skipped += 1

    if cur is not None:
        parsed += 1
        yield ("entry", (cur[0], cur[1], cur[2]))
    yield ("stats", {"entries": parsed, "skipped": skipped})
