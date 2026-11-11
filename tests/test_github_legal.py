import sqlite3

import pytest

from app import github_legal as legal
from app.db import connect


def test_license_requires_explicit_recognizable_open_license():
    assert legal._license_id("MIT License\\nPermission is hereby granted, free of charge...", "MIT") == "MIT"
    assert legal._license_id("Apache License\\nVersion 2.0", "Apache-2.0") == "APACHE-2.0"
    assert legal._license_id("All rights reserved", "MIT") is None
    assert legal._license_id("", "MIT") is None
    assert legal._license_id("Creative Commons Attribution-NonCommercial 4.0", "CC-BY-4.0") is None


def test_readme_content_exclusion_requires_review():
    assert legal._has_scope_exclusions("Music files are not covered by this license.")
    assert not legal._has_scope_exclusions("This repository is licensed under MIT.")


def _legacy_db(path, local_path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
          CREATE TABLE github_repositories(
            id INTEGER PRIMARY KEY, full_name TEXT, html_url TEXT, default_branch TEXT,
            description TEXT DEFAULT '', license_spdx TEXT DEFAULT '', license_name TEXT DEFAULT '',
            stars INTEGER DEFAULT 0, added_at TEXT, last_scanned_at TEXT
          );
          CREATE TABLE github_files(
            id INTEGER PRIMARY KEY, repository_id INTEGER, path TEXT, name TEXT, extension TEXT,
            distribution_mode TEXT DEFAULT 'hosted', rights_status TEXT DEFAULT 'redistributable',
            download_status TEXT DEFAULT 'hosted', local_path TEXT
          );
          INSERT INTO github_repositories(id,full_name,html_url,default_branch,added_at)
            VALUES(1,'old/score','https://github.com/old/score','main','2026-01-01');
        """)
        conn.execute(
            """INSERT INTO github_files
               (id,repository_id,path,name,extension,local_path)
               VALUES(1,1,'score.mxl','score.mxl','.mxl',?)""",
            (str(local_path),),
        )


def test_legacy_database_is_fail_closed_and_local_copy_is_removed(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.sqlite3"
    download_root = tmp_path / "downloads"
    score = download_root / "github" / "1" / "score.mxl"
    score.parent.mkdir(parents=True)
    score.write_bytes(b"legacy score")
    _legacy_db(db_path, score)
    monkeypatch.setattr(legal, "DB_PATH", str(db_path))
    monkeypatch.setattr(legal, "DOWNLOAD_DIR", download_root)

    result = legal.reconcile_database()

    with connect(str(db_path)) as conn:
        repo = conn.execute("SELECT legal_status FROM github_repositories WHERE id=1").fetchone()
        row = conn.execute("SELECT local_path,distribution_mode,download_status FROM github_files WHERE id=1").fetchone()
    assert repo["legal_status"] == "restricted"
    assert row["local_path"] is None
    assert row["distribution_mode"] == "external_link"
    assert row["download_status"] == "external_only"
    assert result["local_files_removed"] == 1
    assert not score.exists()


def test_processing_requires_approved_repository(tmp_path, monkeypatch):
    db_path = tmp_path / "admission.sqlite3"
    _legacy_db(db_path, tmp_path / "unused.mxl")
    monkeypatch.setattr(legal, "DB_PATH", str(db_path))
    with pytest.raises(ValueError, match="Repositorio bloqueado"):
        legal.assert_repository_approved(1)
    with connect(str(db_path)) as conn:
        conn.execute("UPDATE github_repositories SET legal_status='approved' WHERE id=1")
    legal.assert_repository_approved(1)
