import io
import os
import sqlite3
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(_TMP / "api.db")

import pytest

import app as app_module
import db

SAMPLE = Path(__file__).parent / "sample.log"


@pytest.fixture()
def client():
    # fresh tables for every test (same DB file created at import time)
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute("DELETE FROM log_entries")
    conn.execute("DELETE FROM log_files")
    conn.commit()
    conn.close()
    return app_module.app.test_client()


def upload(client, name="sample.log", data=None):
    if data is None:
        data = SAMPLE.read_bytes()
    return client.post(
        "/api/upload",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )


# --- pages & upload ---

def test_index(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Log Analyser" in res.data


def test_upload_sample(client):
    res = upload(client)
    assert res.status_code == 200
    body = res.get_json()
    assert body["filename"] == "sample.log"
    assert body["parsed"] == 11
    assert body["skipped"] == 5
    assert isinstance(body["file_id"], int)


def test_upload_wrong_extension(client):
    res = upload(client, name="evil.exe")
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_upload_missing_file(client):
    res = client.post("/api/upload", data={})
    assert res.status_code == 400


def test_upload_oversized(client):
    big = b"x" * (10 * 1024 * 1024 + 1)
    res = upload(client, name="big.log", data=big)
    assert res.status_code == 413
    assert "error" in res.get_json()


# --- /api/logs ---

def test_logs_filters(client):
    fid = upload(client).get_json()["file_id"]

    res = client.get("/api/logs", query_string={"file_id": fid, "level": "ERROR"})
    assert res.get_json()["total"] == 3

    res = client.get(
        "/api/logs",
        query_string={"file_id": fid, "level": ["ERROR", "WARN"]},
    )
    assert res.get_json()["total"] == 5

    res = client.get("/api/logs", query_string={"file_id": fid, "q": "upstream"})
    assert res.get_json()["total"] == 2

    res = client.get(
        "/api/logs",
        query_string={
            "file_id": fid,
            "start": "2026-11-18T14:33:00Z",
            "end": "2026-11-18T14:41:00Z",
        },
    )
    assert res.get_json()["total"] == 6


def test_logs_pagination(client):
    fid = upload(client).get_json()["file_id"]
    res = client.get(
        "/api/logs", query_string={"file_id": fid, "limit": 5, "offset": 5}
    )
    body = res.get_json()
    assert body["total"] == 11
    assert len(body["rows"]) == 5


def test_logs_bad_params(client):
    assert client.get("/api/logs?limit=abc").status_code == 400
    assert client.get("/api/logs?offset=abc").status_code == 400
    assert client.get("/api/logs?file_id=abc").status_code == 400


# --- /api/metrics ---

def test_metrics(client):
    fid = upload(client).get_json()["file_id"]
    body = client.get("/api/metrics", query_string={"file_id": fid}).get_json()
    assert body["total"] == 11
    assert body["by_level"]["ERROR"] == 3
    assert body["series"]
    assert len(body["top_messages"]) == 4


# --- files ---

def test_files_and_delete(client):
    fid = upload(client).get_json()["file_id"]

    files = client.get("/api/files").get_json()
    assert len(files) == 1
    assert files[0]["entry_count"] == 11

    res = client.delete(f"/api/files/{fid}")
    assert res.status_code == 200
    assert client.get("/api/logs", query_string={"file_id": fid}).get_json()["total"] == 0

    assert client.delete(f"/api/files/{fid}").status_code == 404
    assert client.delete("/api/files/99999").status_code == 404
