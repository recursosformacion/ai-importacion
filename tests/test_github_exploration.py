from __future__ import annotations

import asyncio
import sqlite3

import app.github_exploration as exploration


def test_exploration_schema_persists_queries_and_seed_repositories(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "discovery.sqlite3"))

    exploration.ensure_schema()
    exploration.ensure_schema()

    with exploration.db() as conn:
        queries = conn.execute("SELECT COUNT(*) FROM github_discovery_queries").fetchone()[0]
        seeds = conn.execute(
            "SELECT COUNT(*) FROM github_discovered_repositories WHERE discovery_query='seed'"
        ).fetchone()[0]
        assert queries == len(exploration.QUERIES)
        assert seeds == len(exploration.SEEDS)
        assert conn.execute(
            "SELECT COUNT(*) FROM github_discovered_repositories WHERE full_name='musetrainer/library'"
        ).fetchone()[0] == 1


def test_musical_threshold_is_not_lowered():
    assert exploration.MIN_MUSICAL_FILES == 11
    assert ".musicxml" in exploration.EXTENSIONS
    assert ".mxl" in exploration.EXTENSIONS
    assert ".mid" in exploration.EXTENSIONS
    assert ".pdf" not in exploration.EXTENSIONS


def test_exploration_static_route_is_not_captured_by_repository_id(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "routes.sqlite3"))
    with TestClient(main.app) as client:
        response = client.get("/github/exploration")

    assert response.status_code == 200
    assert "Exploración persistente de GitHub" in response.text



def test_inspection_skips_error_repositories_instead_of_retrying_them_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "inspection.sqlite3"))
    exploration.ensure_schema()

    with exploration.db() as conn:
        conn.execute("UPDATE github_discovered_repositories SET status='below_threshold'")
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('example/failed','https://github.com/example/failed','seed','2026-01-01','error')"""
        )
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('example/pending','https://github.com/example/pending','seed','2026-01-02','pending')"""
        )

    async def fake_get_json(client, url):
        if url.endswith("/repos/example/pending"):
            return {"default_branch": "main", "html_url": "https://github.com/example/pending"}
        if url.endswith("/commits/main"):
            return {"sha": "commit-sha", "commit": {"tree": {"sha": "tree-sha"}}}
        if "/git/trees/main?recursive=1" in url:
            return {"tree": [
                {"type": "blob", "path": f"score-{index}.mxl"}
                for index in range(exploration.MIN_MUSICAL_FILES)
            ]}
        raise AssertionError(f"Unexpected GitHub URL: {url}")

    monkeypatch.setattr(exploration, "get_json", fake_get_json)
    did_work, message = asyncio.run(exploration.inspect_one(object()))

    assert did_work is True
    assert "example/pending" in message
    with exploration.db() as conn:
        statuses = {
            row["full_name"]: row["status"]
            for row in conn.execute(
                "SELECT full_name,status FROM github_discovered_repositories"
                " WHERE full_name IN ('example/failed','example/pending')"
            )
        }
    assert statuses == {
        "example/failed": "error",
        "example/pending": "qualified",
    }



def test_exploration_budget_has_no_arbitrary_twenty_minute_cap(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "budget.sqlite3"))
    with TestClient(main.app) as client:
        response = client.get("/github/exploration")

    assert response.status_code == 200
    assert 'name="minutes" min=\'1\' value=\'60\'' in response.text
    assert "max='20'" not in response.text
