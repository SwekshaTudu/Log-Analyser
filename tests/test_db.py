import sqlite3
from pathlib import Path

import pytest

import db
from parser import iter_parse

SAMPLE = Path(__file__).parent / "sample.log"


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return tmp_path / "test.db"


def load_sample():
    with SAMPLE.open(encoding="utf-8") as f:
        return list(iter_parse(f))


@pytest.fixture()
def sample_file(test_db):
    file_id = db.insert_file("sample.log")
    stats = db.insert_entries(file_id, load_sample())
    assert stats == {"entries": 11, "skipped": 5}
    return file_id


def raw_count():
    conn = sqlite3.connect(db._db_path())
    try:
        return conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
    finally:
        conn.close()


# --- insert / files ---

def test_insert_and_list_files(sample_file):
    files = db.list_files()
    assert len(files) == 1
    assert files[0]["filename"] == "sample.log"
    assert files[0]["entry_count"] == 11
    assert files[0]["uploaded_at"].endswith("Z")


def test_reupload_same_filename_allowed(sample_file):
    fid2 = db.insert_file("sample.log")
    assert fid2 != sample_file
    assert len(db.list_files()) == 2


# --- query_entries filters ---

def test_level_filter(sample_file):
    res = db.query_entries(file_id=sample_file, level="ERROR")
    assert res["total"] == 3
    assert {r["level"] for r in res["rows"]} == {"ERROR"}


def test_level_multiple_values(sample_file):
    res = db.query_entries(file_id=sample_file, level=["ERROR", "WARN"])
    assert res["total"] == 5


def test_date_range_filter(sample_file):
    # span covers FATAL(14:33) through syslog entries(14:40);
    # the +02:00 WARN normalizes to 12:35Z and is excluded
    res = db.query_entries(
        file_id=sample_file,
        start="2026-11-18T14:33:00Z",
        end="2026-11-18T14:41:00Z",
    )
    assert res["total"] == 6
    stamps = [r["timestamp"] for r in res["rows"]]
    assert stamps == sorted(stamps)


def test_keyword_filter(sample_file):
    res = db.query_entries(file_id=sample_file, q="upstream")
    assert res["total"] == 2


def test_keyword_with_literal_percent_and_underscore(sample_file, test_db):
    events = [
        ("entry", ("2026-01-01T00:00:00Z", "INFO", "cpu at 100% load_1 ok")),
        ("entry", ("2026-01-01T00:00:01Z", "INFO", "cpu at 1000 loadX ok")),
        ("stats", {"entries": 2, "skipped": 0}),
    ]
    fid = db.insert_file("pct.log")
    db.insert_entries(fid, events)

    # literal % must not act as a wildcard
    res = db.query_entries(file_id=fid, q="100%")
    assert res["total"] == 1
    assert res["rows"][0]["message"].startswith("cpu at 100%")

    # literal _ must not act as a wildcard
    res = db.query_entries(file_id=fid, q="load_1")
    assert res["total"] == 1

    # a bare % only matches the row containing a literal %
    res = db.query_entries(file_id=fid, q="%")
    assert res["total"] == 1


def test_pagination(sample_file):
    page1 = db.query_entries(file_id=sample_file, limit=5, offset=0)
    page2 = db.query_entries(file_id=sample_file, limit=5, offset=5)
    page3 = db.query_entries(file_id=sample_file, limit=5, offset=10)
    assert page1["total"] == page2["total"] == page3["total"] == 11
    assert len(page1["rows"]) == 5
    assert len(page2["rows"]) == 5
    assert len(page3["rows"]) == 1
    ids1 = {r["id"] for r in page1["rows"]}
    ids2 = {r["id"] for r in page2["rows"]}
    assert not ids1 & ids2
    # sorted by timestamp then id
    stamps = [r["timestamp"] for r in page1["rows"]]
    assert stamps == sorted(stamps)


def test_limit_capped(sample_file):
    res = db.query_entries(file_id=sample_file, limit=99999)
    assert len(res["rows"]) <= db.MAX_LIMIT


# --- query_metrics ---

def test_metrics(sample_file):
    m = db.query_metrics(file_id=sample_file)
    assert m["total"] == 11
    assert m["by_level"] == {
        "DEBUG": 2, "ERROR": 3, "FATAL": 1, "INFO": 3, "WARN": 2,
    }
    # span ~2h -> minute buckets
    assert m["series"], "expected series buckets"
    assert all("bucket" in b and "count" in b and "errors" in b
               for b in m["series"])
    total_in_series = sum(b["count"] for b in m["series"])
    assert total_in_series == 11
    # 4 distinct ERROR/FATAL first lines, each seen once
    assert len(m["top_messages"]) == 4
    assert all(t["count"] == 1 for t in m["top_messages"])
    # first line of the stack-trace entry, not a traceback line
    msgs = {t["first_line"] for t in m["top_messages"]}
    assert "connection refused to db.internal:5432" in msgs
    assert not any("Traceback" in x for x in msgs)


def test_metrics_with_level_filter(sample_file):
    m = db.query_metrics(file_id=sample_file, level="ERROR")
    assert m["total"] == 3
    assert m["by_level"] == {"ERROR": 3}
    assert all(t["count"] <= 3 for t in m["top_messages"])


def test_metrics_empty_result(test_db):
    m = db.query_metrics(q="no-such-keyword")
    assert m["total"] == 0
    assert m["by_level"] == {}
    assert m["series"] == []
    assert m["top_messages"] == []


# --- delete / cascade ---

def test_delete_file_cascades_entries(sample_file):
    assert raw_count() == 11
    assert db.delete_file(sample_file) is True
    assert raw_count() == 0  # entries really gone -> FK cascade worked
    assert db.query_entries(file_id=sample_file)["total"] == 0
    assert db.list_files() == []
    assert db.delete_file(sample_file) is False
