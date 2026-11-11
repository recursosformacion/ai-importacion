import asyncio
import sqlite3

import httpx

from app.license_recheck import ensure_schema, recheck_due


def _database(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE github_repositories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              full_name TEXT NOT NULL UNIQUE,
              license_spdx TEXT NOT NULL DEFAULT '',
              license_name TEXT NOT NULL DEFAULT '',
              license_checked_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO github_repositories(full_name, license_spdx, license_name) "
            "VALUES('example/scores', 'cc0-1.0', 'CC0 1.0')"
        )
    ensure_schema(str(path))
    return path


def _repo_row(path, repository_id=1):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM github_repositories WHERE id=?", (repository_id,)
        ).fetchone()


def test_license_change_to_non_hostable_marks_repository_link_only(tmp_path):
    path = _database(tmp_path)

    def handler(request):
        return httpx.Response(
            200, json={"license": {"spdx_id": "CC-BY-NC-4.0", "name": "CC BY-NC 4.0"}}
        )

    results = asyncio.run(
        recheck_due(db_path=str(path), force=True, transport=httpx.MockTransport(handler))
    )

    assert len(results) == 1
    assert results[0]["status"] == "link_only"
    assert results[0]["changed"] is True
    row = _repo_row(path)
    assert row["license_spdx"] == "cc-by-nc-4.0"
    assert row["license_status"] == "link_only"
    assert row["license_previous_spdx"] == "cc0-1.0"
    assert row["license_changed_at"]

    with sqlite3.connect(path) as conn:
        history = conn.execute(
            "SELECT spdx_before, spdx_after, status, changed FROM github_license_checks"
        ).fetchall()
    assert history == [("cc0-1.0", "cc-by-nc-4.0", "link_only", 1)]


def test_removed_license_becomes_unknown_and_not_hostable(tmp_path):
    path = _database(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"license": None})

    results = asyncio.run(
        recheck_due(db_path=str(path), force=True, transport=httpx.MockTransport(handler))
    )

    assert results[0]["status"] == "unknown"
    assert _repo_row(path)["license_status"] == "unknown"


def test_recent_check_is_skipped_unless_forced(tmp_path):
    path = _database(tmp_path)
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"license": {"spdx_id": "cc0-1.0"}})

    first = asyncio.run(
        recheck_due(db_path=str(path), force=True, transport=httpx.MockTransport(handler))
    )
    second = asyncio.run(
        recheck_due(db_path=str(path), days=7, transport=httpx.MockTransport(handler))
    )

    assert len(first) == 1
    assert second == []
    assert len(calls) == 1


def test_missing_repository_is_link_only(tmp_path):
    path = _database(tmp_path)

    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    results = asyncio.run(
        recheck_due(db_path=str(path), force=True, transport=httpx.MockTransport(handler))
    )

    assert results[0]["status"] == "unknown"
    assert "no encontrado" in results[0]["note"]
    assert _repo_row(path)["license_status"] == "unknown"


def test_rate_limit_stops_without_writing_a_false_change(tmp_path):
    path = _database(tmp_path)

    def handler(request):
        return httpx.Response(403, json={"message": "rate limit"})

    results = asyncio.run(
        recheck_due(db_path=str(path), force=True, transport=httpx.MockTransport(handler))
    )

    assert results[0]["http_status"] == 403
    assert results[0]["changed"] is False
    row = _repo_row(path)
    assert row["license_spdx"] == "cc0-1.0"
    assert row["license_status"] == "hostable"


def test_dry_run_does_not_write(tmp_path):
    path = _database(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"license": None})

    results = asyncio.run(
        recheck_due(
            db_path=str(path),
            force=True,
            dry_run=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert results[0]["status"] == "unknown"
    assert _repo_row(path)["license_status"] == "hostable"
