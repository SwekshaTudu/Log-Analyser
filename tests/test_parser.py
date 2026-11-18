from datetime import datetime, timezone
from pathlib import Path

from parser import iter_parse

SAMPLE = Path(__file__).parent / "sample.log"


def run(lines):
    """Return (entries, stats) from iter_parse."""
    events = list(iter_parse(lines))
    assert events[-1][0] == "stats"
    entries = [p for k, p in events if k == "entry"]
    return entries, events[-1][1]


def entry(lines):
    entries, stats = run(lines)
    assert stats["entries"] == len(entries)
    return entries[0]


# --- format A: "YYYY-MM-DD HH:MM:SS LEVEL message" ---

def test_format_a_naive_assumed_utc():
    ts, level, msg = entry(["2026-11-18 14:32:01 INFO server started\n"])
    assert ts == "2026-11-18T14:32:01Z"
    assert level == "INFO"
    assert msg == "server started"


# --- format B: "[LEVEL] ISO8601 message" ---

def test_format_b_zulu():
    ts, level, msg = entry(["[INFO] 2026-11-18T14:35:12Z health ok\n"])
    assert ts == "2026-11-18T14:35:12Z"
    assert level == "INFO"


def test_format_b_positive_offset_converted_to_utc():
    ts, _, _ = entry(["[WARN] 2026-11-18T14:35:41+02:00 cache miss\n"])
    assert ts == "2026-11-18T12:35:41Z"


def test_format_b_fractional_seconds_dropped():
    ts, _, _ = entry(["[ERROR] 2026-11-18T14:35:40.250+00:00 timeout\n"])
    assert ts == "2026-11-18T14:35:40Z"


# --- format C: syslog ---

def test_format_c_syslog_current_year():
    ts, level, msg = entry(
        ["Nov 18 14:40:01 web01 nginx[812]: INFO GET / 200\n"]
    )
    year = datetime.now(timezone.utc).year
    assert ts == f"{year}-11-18T14:40:01Z"
    assert level == "INFO"
    assert msg == "GET / 200"


# --- level normalization ---

def test_level_normalization():
    _, lvl1, _ = entry(["2026-11-18 14:32:01 WARNING watch out\n"])
    _, lvl2, _ = entry(["[CRITICAL] 2026-11-18T14:35:12Z boom\n"])
    assert lvl1 == "WARN"
    assert lvl2 == "FATAL"


def test_unknown_level_not_an_entry():
    _, stats = run(["2026-11-18 14:32:01 VERBOSE chatter\n"])
    assert stats["entries"] == 0
    assert stats["skipped"] == 1


# --- continuation / stack traces ---

def test_stack_trace_appended_to_previous_entry():
    lines = [
        "2026-11-18 14:32:05 ERROR connection refused\n",
        "Traceback (most recent call last):\n",
        '  File "app.py", line 42, in connect\n',
        '    raise ConnectionError("unreachable")\n',
        "ConnectionError: unreachable\n",
        "Caused by: java.net.SocketTimeoutException\n",
        "2026-11-18 14:32:06 WARN retrying\n",
    ]
    entries, stats = run(lines)
    assert stats["entries"] == 2
    assert stats["skipped"] == 0
    msg = entries[0][2]
    assert "connection refused" in msg
    assert "Traceback (most recent call last):" in msg
    assert '  File "app.py", line 42, in connect' in msg
    assert "ConnectionError: unreachable" in msg
    assert "Caused by: java.net.SocketTimeoutException" in msg
    assert entries[1][2] == "retrying"


def test_malformed_line_resets_previous_entry():
    # an indented line after a skipped line is an orphan -> skipped,
    # not appended to the entry before the garbage
    lines = [
        "2026-11-18 14:32:01 INFO start\n",
        "garbage line with no format\n",
        "    orphaned indent\n",
        "2026-11-18 14:32:02 INFO next\n",
    ]
    entries, stats = run(lines)
    assert stats == {"entries": 2, "skipped": 2}
    assert "orphaned indent" not in entries[0][2]


def test_continuation_before_first_entry_is_skipped():
    _, stats = run(["    orphan indented line\n"])
    assert stats["entries"] == 0
    assert stats["skipped"] == 1


# --- malformed / skipped ---

def test_malformed_lines_skipped():
    lines = [
        "2026-11-18 14:32:01 INFO ok\n",
        "garbage line with no format\n",
        "???\n",
        "!!! corrupted !!!\n",
        "2026-13-45 99:99:99 INFO invalid date\n",
    ]
    entries, stats = run(lines)
    assert stats["entries"] == 1
    assert stats["skipped"] == 4
    # malformed lines do not break the stream
    assert entries[0][1] == "INFO"


def test_unindented_error_like_line_is_continuation():
    # "ValueError: ..." matches the continuation regex even unindented
    entries, stats = run(
        [
            "2026-11-18 14:32:01 INFO start\n",
            "ValueError: something broke\n",
        ]
    )
    assert stats["skipped"] == 0
    assert "ValueError: something broke" in entries[0][2]


def test_blank_lines_ignored():
    entries, stats = run(
        ["\n", "   \n", "2026-11-18 14:32:01 INFO ok\n", "\n"]
    )
    assert stats["entries"] == 1
    assert stats["skipped"] == 0


def test_empty_input():
    entries, stats = run([])
    assert entries == []
    assert stats == {"entries": 0, "skipped": 0}


# --- sample.log integration ---

def test_sample_log():
    with SAMPLE.open(encoding="utf-8") as f:
        entries, stats = run(f)
    assert stats["entries"] == 11
    assert stats["skipped"] == 5
    levels = [e[1] for e in entries]
    assert levels.count("ERROR") == 3
    # stack trace folded into the first ERROR entry
    assert "Traceback" in entries[1][2]
    # +02:00 offset normalized to UTC
    assert entries[7][0] == "2026-11-18T12:35:41Z"
    # invalid-date line counted as skipped, not an entry
    assert all(e[0] != "2026-13-45T99:99:99Z" for e in entries)
