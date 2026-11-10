from __future__ import annotations

import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.github_exploration as exploration


def exploration_test_client(*, include_catalog: bool = False) -> TestClient:
    """Build a lightweight app without app.main's database init and workers."""
    app = FastAPI()
    app.include_router(exploration.router)
    if include_catalog:
        # Keep the same order as production: static exploration routes first,
        # then the /github/{repository_id} routes.
        from app.github_catalog import router as github_catalog_router

        app.include_router(github_catalog_router)
    return TestClient(app)


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


def test_musical_repository_threshold_is_more_than_ten():
    assert exploration.MIN_MUSICAL_FILES == 11
    assert ".musicxml" in exploration.EXTENSIONS
    assert ".mxl" in exploration.EXTENSIONS
    assert ".mid" in exploration.EXTENSIONS
    assert ".pdf" not in exploration.EXTENSIONS


def test_exploration_static_route_is_not_captured_by_repository_id(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "routes.sqlite3"))
    with exploration_test_client(include_catalog=True) as client:
        response = client.get("/github/exploration")

    assert response.status_code == 200
    assert "Fuentes GitHub" in response.text
    assert "1. Buscar repositorios" in response.text
    assert "2. Seleccionar repositorios" in response.text
    assert "Descubrir colecciones musicales en GitHub" in response.text


def test_repository_selection_is_restored_and_submitted_across_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "selection.sqlite3"))
    exploration.ensure_schema()
    with exploration.db() as conn:
        for name, count in (("example/one", 150), ("example/two", 140)):
            conn.execute(
                """INSERT INTO github_discovered_repositories
                   (full_name,html_url,discovery_query,discovered_at,status,musical_file_count)
                   VALUES(?,?, 'test','2026-01-01','qualified',?)""",
                (name, "https://github.com/" + name, count),
            )

    with exploration_test_client() as client:
        response = client.get("/github/exploration")
    assert response.status_code == 200
    assert "data-select-visible" in response.text
    assert "ai-importacion.github-exploration.selected.v1" in response.text
    assert "data-persisted-selection" in response.text
    assert "repo-selection-count" in response.text
    assert "Seleccionar todos los visibles" in response.text


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
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "budget.sqlite3"))
    with exploration_test_client() as client:
        response = client.get("/github/exploration")

    assert response.status_code == 200
    assert "name='minutes' min='1' value='60'" in response.text
    assert "max='20'" not in response.text


def test_bulk_repository_actions_ignore_process_and_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "bulk-actions.sqlite3"))
    exploration.ensure_schema()
    with exploration.db() as conn:
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('example/one','https://github.com/example/one','test','2026-01-01','pending')"""
        )
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('example/two','https://github.com/example/two','test','2026-01-02','qualified')"""
        )
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('example/three','https://github.com/example/three','test','2026-01-03','qualified')"""
        )

    with exploration_test_client() as client:
        ignored = client.post(
            "/github/exploration/repositories/action",
            data={"action": "ignore", "repositories": ["example/one", "example/two"]},
            follow_redirects=False,
        )
        assert ignored.status_code == 303
        with exploration.db() as conn:
            statuses = {
                row["full_name"]: row["status"]
                for row in conn.execute(
                    "SELECT full_name,status FROM github_discovered_repositories "
                    "WHERE full_name LIKE 'example/%'"
                )
            }
        assert statuses == {"example/one": "ignored", "example/two": "ignored", "example/three": "qualified"}

        restored = client.post(
            "/github/exploration/repositories/action",
            data={"action": "restore", "repositories": ["example/one"]},
            follow_redirects=False,
        )
        assert restored.status_code == 303
        with exploration.db() as conn:
            status = conn.execute(
                "SELECT status FROM github_discovered_repositories WHERE full_name='example/one'"
            ).fetchone()["status"]
        assert status == "pending"

        processed = client.post(
            "/github/exploration/repositories/action",
            data={"action": "process", "repositories": ["example/three"]},
            follow_redirects=False,
        )
        assert processed.status_code == 303
        with exploration.db() as conn:
            status = conn.execute(
                "SELECT status FROM github_discovered_repositories WHERE full_name='example/three'"
            ).fetchone()["status"]
        assert status == "pending"

def test_selected_discovered_repository_can_be_imported_into_catalog(tmp_path, monkeypatch):
    import app.github_catalog as catalog
    import app.github_processing as processing

    monkeypatch.setattr(processing, "enqueue_repositories", lambda repository_ids: None)
    db_path = str(tmp_path / "catalog-import.sqlite3")
    monkeypatch.setattr(exploration, "DB_PATH", db_path)
    monkeypatch.setattr(catalog, "DB_PATH", db_path)
    exploration.ensure_schema()
    with exploration.db() as conn:
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,default_branch,description,stars,license_spdx,
                discovery_query,discovered_at,status,musical_file_count,last_checked_at)
               VALUES('OpenScore/Lieder-test-fixture','https://github.com/OpenScore/Lieder-test-fixture','main',
                      'Open score collection',42,'CC-BY-4.0','seed','2026-01-01','qualified',135,'2026-01-01')"""
        )
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,default_branch,description,stars,license_spdx,
                discovery_query,discovered_at,status,musical_file_count,last_checked_at)
               VALUES('example/two-pdfs','https://github.com/example/two-pdfs','main',
                      'Only two PDF documents',0,'','test','2026-01-02','below_threshold',0,'2026-01-02')"""
        )

    with exploration_test_client() as client:
        response = client.post(
            "/github/exploration/repositories/action",
            data={"action": "catalog", "repositories": ["OpenScore/Lieder-test-fixture", "example/two-pdfs"]},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "catalog_added=1" in response.headers["location"]
    assert "catalog_skipped=1" in response.headers["location"]
    catalog.ensure_schema()
    with catalog.db() as conn:
        rows = conn.execute(
            "SELECT full_name,default_branch,license_spdx FROM github_repositories"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["full_name"] == "OpenScore/Lieder-test-fixture"
    assert row["default_branch"] == "main"
    assert row["license_spdx"] == "CC-BY-4.0"



def test_exploration_table_shows_count_and_only_lists_repositories_at_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "visible-counts.sqlite3"))
    exploration.ensure_schema()
    with exploration.db() as conn:
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,description,discovery_query,discovered_at,status,
                musical_file_count,last_checked_at)
               VALUES('example/valid','https://github.com/example/valid','Valid repo',
                      'test','2026-01-03','qualified',117,'2026-01-03')"""
        )
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status,
                musical_file_count,last_checked_at)
               VALUES('example/too-small','https://github.com/example/too-small',
                      'test','2026-01-02','below_threshold',10,'2026-01-02')"""
        )
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status)
               VALUES('example/not-inspected','https://github.com/example/not-inspected',
                      'test','2026-01-01','pending')"""
        )

    with exploration_test_client() as client:
        response = client.get("/github/exploration")

    assert response.status_code == 200
    assert "example/valid" in response.text
    assert "<b>117</b>" in response.text
    assert "example/too-small" not in response.text
    assert "example/not-inspected" not in response.text
    assert "Sin inspeccionar" not in response.text


def test_schema_reclassifies_old_qualified_repositories_below_new_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(exploration, "DB_PATH", str(tmp_path / "threshold-migration.sqlite3"))
    exploration.ensure_schema()
    with exploration.db() as conn:
        # Simulate a database created before the threshold migration ran.
        conn.execute("DELETE FROM github_exploration_migrations WHERE migration_key='repository_threshold_11_v2'")
        conn.execute(
            """INSERT INTO github_discovered_repositories
               (full_name,html_url,discovery_query,discovered_at,status,
                musical_file_count,last_checked_at)
               VALUES('example/old-qualified','https://github.com/example/old-qualified',
                      'test','2026-01-01','qualified',10,'2026-01-01')"""
        )
    exploration.ensure_schema()
    with exploration.db() as conn:
        row = conn.execute(
            "SELECT status,below_threshold FROM github_discovered_repositories "
            "WHERE full_name='example/old-qualified'"
        ).fetchone()
    assert row["status"] == "below_threshold"
    assert row["below_threshold"] == 1
