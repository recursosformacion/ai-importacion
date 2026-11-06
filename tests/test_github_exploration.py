from __future__ import annotations

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
