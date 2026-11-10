import json

from fastapi.testclient import TestClient

import app.analysis_engine as engine
import app.github_catalog as github_catalog
import app.main as main


SCORE = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <work><work-title>GitHub Test Score</work-title></work>
  <identification><creator type="composer">Test Composer</creator></identification>
  <part-list><score-part id="P1"><part-name>Soprano</part-name></score-part></part-list>
  <part id="P1"><measure number="1"/></part>
</score-partwise>"""


def test_shared_engine_analyzes_musicxml_without_provider_context(tmp_path):
    path = tmp_path / "score.musicxml"
    path.write_text(SCORE, encoding="utf-8")

    result = engine.analyze_local_music_file(path)

    assert result["analysis_type"] == "musicxml"
    assert result["status"] == "ok"
    assert result["analysis"]["title"] == "GitHub Test Score"
    assert result["error_text"] is None


def test_shared_engine_routes_midi_to_midi_analyzer(tmp_path, monkeypatch):
    path = tmp_path / "score.mid"
    path.write_bytes(b"MThd")
    called = []

    def fake_analyze(source):
        called.append(source)
        return {"format": "midi", "status": "ok", "track_count": 1}

    monkeypatch.setattr(engine, "analyze_midi", fake_analyze)

    result = engine.analyze_local_music_file(path)

    assert called == [path]
    assert result["analysis_type"] == "midi"
    assert result["status"] == "ok"


def test_shared_engine_reports_missing_local_file(tmp_path):
    result = engine.analyze_local_music_file(tmp_path / "missing.musicxml")

    assert result["status"] == "error"
    assert "no existe" in result["error_text"].lower()


def test_github_analysis_route_persists_results_separately(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-analysis.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)
    score_path = tmp_path / "github-score.musicxml"
    score_path.write_text(SCORE, encoding="utf-8")

    with TestClient(main.app) as client:
        github_catalog.ensure_schema()
        with github_catalog.db() as conn:
            repository_id = conn.execute(
                """INSERT INTO github_repositories
                   (full_name,html_url,default_branch,added_at)
                   VALUES(?,?,?,?)""",
                ("test-owner/test-repo", "https://github.com/test-owner/test-repo",
                 "main", main.now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO github_files
                   (repository_id,path,name,extension,size_bytes,sha,html_url,first_seen_at,last_seen_at,
                    download_status,local_path,sha256,downloaded_bytes)
                   VALUES(?,?,?,?,?,?,?,?,?,'downloaded',?,?,?)""",
                (repository_id, "scores/test.musicxml", "test.musicxml", ".musicxml",
                 score_path.stat().st_size, "git-blob-sha",
                 "https://github.com/test-owner/test-repo/blob/main/scores/test.musicxml",
                 main.now(), main.now(), str(score_path), "content-sha256", score_path.stat().st_size),
            )

        response = client.post(f"/github/{repository_id}/analyze", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == f"/github/{repository_id}?analysis=done"

    with github_catalog.db() as conn:
        row = conn.execute(
            """SELECT a.status,a.sha256,a.analysis_json
               FROM github_file_analyses a JOIN github_files f ON f.id=a.github_file_id
               WHERE f.repository_id=?""",
            (repository_id,),
        ).fetchone()

    assert row["status"] == "ok"
    assert row["sha256"] == "content-sha256"
    assert "GitHub Test Score" in row["analysis_json"]
    with github_catalog.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0] == 0
