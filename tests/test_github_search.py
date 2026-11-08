import asyncio

from fastapi.testclient import TestClient

import app.github_catalog as github_catalog
import app.main as main
import app.github_exploration as exploration


def test_github_repository_search_queues_custom_query(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-search.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)
    monkeypatch.setattr(exploration, "DB_PATH", db_path)
    monkeypatch.setattr(exploration, "start_exploration_worker", lambda: None)

    with TestClient(main.app) as client:
        response = client.get(
            "/github/search",
            params={"q": "choral musicxml", "sort": "stars", "license": "cc-by-4.0"},
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/github/exploration"
    with exploration.db() as conn:
        row = conn.execute(
            "SELECT page,status FROM github_discovery_queries WHERE query=?",
            ("choral musicxml license:cc-by-4.0",),
        ).fetchone()
        state = conn.execute(
            "SELECT job_status,last_message FROM github_exploration_state WHERE id=1"
        ).fetchone()
    assert tuple(row) == (1, "pending")
    assert state["job_status"] == "queued"
    assert "choral musicxml" in state["last_message"]


def test_github_search_without_query_queues_all_automatic_discovery_families(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-auto-search.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)
    monkeypatch.setattr(exploration, "DB_PATH", db_path)
    monkeypatch.setattr(exploration, "start_exploration_worker", lambda: None)

    with TestClient(main.app) as client:
        response = client.get("/github/search")

    assert response.status_code == 303
    assert response.headers["location"] == "/github/exploration"
    with exploration.db() as conn:
        queued = {
            row["query"]: row["status"]
            for row in conn.execute("SELECT query,status FROM github_discovery_queries")
        }
        state = conn.execute(
            "SELECT job_status,last_message FROM github_exploration_state WHERE id=1"
        ).fetchone()
    assert set(exploration.QUERIES).issubset(queued)
    assert all(queued[query] == "pending" for query in exploration.QUERIES)
    assert state["job_status"] == "queued"
    assert "Búsqueda automática" in state["last_message"]


def test_github_inventory_recognizes_multiple_score_and_audio_formats():
    expected = {
        ".musicxml", ".mxl", ".mid", ".kar", ".krn", ".abc", ".ly",
        ".mei", ".mscz", ".capx", ".sib", ".gp5", ".gpx",
        ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".pdf",
    }
    assert expected.issubset(github_catalog.MUSIC_EXTENSIONS)
    assert ".txt" not in github_catalog.MUSIC_EXTENSIONS
    assert ".xml" in github_catalog.MUSIC_EXTENSIONS
    assert ".txt" not in github_catalog.MUSICAL_PIECE_EXTENSIONS
    assert ".xml" not in github_catalog.MUSICAL_PIECE_EXTENSIONS
    assert github_catalog.MIN_MUSICAL_PIECES == 11



def test_repository_with_ten_music_files_is_below_the_100_file_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "github-threshold.sqlite3"))
    exploration.ensure_schema()
    with exploration.db() as conn:
        conn.execute("UPDATE github_discovered_repositories SET status='ignored'")
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('Example/TestOnly','https://github.com/Example/TestOnly',
                      'test','2026-11-05','pending')"""
        )

    async def fake_get_json(client, url):
        if url.endswith("/repos/Example/TestOnly"):
            return {
                "default_branch": "main",
                "html_url": "https://github.com/Example/TestOnly",
                "description": "Test fixtures",
                "stargazers_count": 1,
                "updated_at": "2026-11-05T12:00:00Z",
                "license": None,
            }
        if url.endswith("/commits/main"):
            return {"sha": "commit-sha", "commit": {"tree": {"sha": "tree-sha"}}}
        if "/git/trees/main?recursive=1" in url:
            return {"tree": [
                {"type": "blob", "path": f"piece-{i}.musicxml"} for i in range(10)
            ] + [
                {"type": "blob", "path": f"test-{i}.txt"} for i in range(100)
            ] + [
                {"type": "blob", "path": f"config-{i}.xml"} for i in range(100)
            ]}
        raise AssertionError(f"Unexpected GitHub URL: {url}")

    monkeypatch.setattr(exploration, "get_json", fake_get_json)
    did_work, message = asyncio.run(exploration.inspect_one(object()))

    assert did_work is True
    assert "10 archivos musicales" in message
    with exploration.db() as conn:
        row = conn.execute(
            "SELECT status,musical_file_count,below_threshold FROM github_discovered_repositories "
            "WHERE full_name='Example/TestOnly'"
        ).fetchone()
    assert tuple(row) == ("below_threshold", 10, 1)


