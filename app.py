"""Flask app: upload, log queries, metrics, file management."""

import io
import os

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest

import db
import parser

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".log", ".txt"}

db.init_db()


@app.errorhandler(400)
def bad_request(e):
    return jsonify(error=e.description), 400


@app.errorhandler(413)
def too_large(e):
    return jsonify(error="file too large (max 10MB)"), 413


def _int_arg(name, default):
    v = request.args.get(name)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        raise BadRequest(f"invalid integer for '{name}': {v!r}")


def _filters():
    levels = [lv for lv in request.args.getlist("level") if lv]
    return {
        "file_id": _int_arg("file_id", None),
        "level": levels or None,
        "start": request.args.get("start") or None,
        "end": request.args.get("end") or None,
        "q": request.args.get("q") or None,
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="missing 'file' upload field"), 400
    if os.path.splitext(f.filename)[1].lower() not in ALLOWED_EXTENSIONS:
        return jsonify(error="only .log and .txt files are accepted"), 400

    file_id = db.insert_file(f.filename)
    try:
        text = io.TextIOWrapper(f.stream, encoding="utf-8", errors="replace")
        stats = db.insert_entries(file_id, parser.iter_parse(text))
    except Exception:
        db.delete_file(file_id)  # insert_entries is one transaction -> nothing partial
        raise
    return jsonify(
        file_id=file_id,
        filename=f.filename,
        parsed=stats["entries"],
        skipped=stats["skipped"],
    )


@app.get("/api/logs")
def logs():
    return jsonify(
        db.query_entries(
            **_filters(),
            limit=_int_arg("limit", db.DEFAULT_LIMIT),
            offset=_int_arg("offset", 0),
        )
    )


@app.get("/api/metrics")
def metrics():
    return jsonify(db.query_metrics(**_filters()))


@app.get("/api/files")
def files():
    return jsonify(db.list_files())


@app.delete("/api/files/<int:file_id>")
def delete_file(file_id):
    if not db.delete_file(file_id):
        return jsonify(error="file not found"), 404
    return jsonify(deleted=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
