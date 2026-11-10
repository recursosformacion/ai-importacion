from __future__ import annotations

import subprocess
from pathlib import Path

import app.github_processing as processing


def test_processing_schema_contains_stage_progress_and_output_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(processing, "DB_PATH", str(tmp_path / "processing.sqlite3"))

    processing.ensure_processing_schema()

    with processing.connect(processing.DB_PATH) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_processing_jobs)")}
    assert {"status", "stage", "progress_count", "total_count", "current_path", "processed_count", "output_dir"} <= columns


def test_resource_record_combines_structural_and_ai_metadata():
    repo = {
        "full_name": "example/scores",
        "html_url": "https://github.com/example/scores",
        "license_spdx": "CC-BY-4.0",
        "license_name": "Creative Commons Attribution 4.0",
    }
    row = {
        "path": "scores/ave-verum.musicxml",
        "name": "ave-verum.musicxml",
        "extension": ".musicxml",
        "sha": "git-sha",
        "html_url": "https://github.com/example/scores/blob/main/scores/ave-verum.musicxml",
        "download_url": "https://raw.githubusercontent.com/example/scores/main/scores/ave-verum.musicxml",
        "downloaded_at": "2026-11-10T10:00:00+00:00",
        "downloaded_bytes": 1200,
        "size_bytes": 1200,
        "sha256": "content-sha",
        "analysis_status": "ok",
        "analysis_json": '{"title":"Ave verum corpus","creators":[{"name":"W. A. Mozart","type":"composer"}]}',
        "ai_metadata_json": '{"status":"ok","title":"Ave verum corpus","creators":[{"name":"W. A. Mozart","type":"composer"}]}',
        "ai_sha256": "content-sha",
        "ai_model": "gemini-flash-lite-latest",
        "analyzed_at": "2026-11-10T10:01:00+00:00",
        "extraction_status": None,
        "extracted_at": None,
    }

    result = processing._resource_record(repo, row)

    assert result["resource"]["title"] == "Ave verum corpus"
    assert result["resource"]["creators"][0]["name"] == "W. A. Mozart"
    assert result["identification"]["model"] == "gemini-flash-lite-latest"
    assert result["source"]["repository"] == "example/scores"


def test_sparse_checkout_materializes_only_selected_music_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "tests@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Tests"], check=True)

    (source / "scores").mkdir()
    (source / "docs").mkdir()
    (source / "scores" / "piece.musicxml").write_text("<score-partwise/>", encoding="utf-8")
    (source / "scores" / "piece.mid").write_bytes(b"MThd")
    (source / "docs" / "README.txt").write_text("not a musical resource", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "fixture"], check=True, capture_output=True)

    checkout = tmp_path / "checkout"
    processing._clone_sparse_checkout(
        str(source), "main",
        ["scores/piece.musicxml", "scores/piece.mid"],
        checkout,
    )

    assert (checkout / "scores" / "piece.musicxml").is_file()
    assert (checkout / "scores" / "piece.mid").is_file()
    assert not (checkout / "docs" / "README.txt").exists()


def test_recover_processing_jobs_requeues_interrupted_work(tmp_path, monkeypatch):
    db_path = str(tmp_path / "recovery.sqlite3")
    monkeypatch.setattr(processing, "DB_PATH", db_path)
    processing.ensure_processing_schema()
    with processing.connect(db_path) as conn:
        conn.execute("CREATE TABLE github_repositories(id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO github_repositories(id) VALUES(7)")
        conn.execute("""
            INSERT INTO github_processing_jobs
              (repository_id,status,stage,progress_count,total_count,started_at,updated_at)
            VALUES(7,'running','analysis',12,100,'start','before-restart')
        """)

    queued = []
    monkeypatch.setattr(processing, "enqueue_repositories", lambda ids: queued.extend(ids))

    recovered = processing.recover_processing_jobs()

    assert recovered == 1
    assert queued == [7]
    with processing.connect(db_path) as conn:
        job = conn.execute(
            "SELECT status,stage,progress_count,current_path FROM github_processing_jobs WHERE repository_id=7"
        ).fetchone()
    assert job["status"] == "queued"
    assert job["stage"] == "queued"
    assert job["progress_count"] == 12
    assert job["current_path"] == ""
