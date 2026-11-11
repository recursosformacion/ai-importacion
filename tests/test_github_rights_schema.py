import app.github_catalog as catalog


def test_github_score_files_keep_repository_license_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "DB_PATH", str(tmp_path / "github-rights.sqlite3"))
    catalog.ensure_schema()
    with catalog.db() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_files)")}
        assert {
            "repository_license_spdx", "repository_license_name",
            "license_spdx", "license_name", "license_url", "license_scope",
            "rights_status", "distribution_mode", "transcriber_credit",
        }.issubset(columns)
        repo_id = conn.execute(
            """INSERT INTO github_repositories
               (full_name,html_url,default_branch,added_at)
               VALUES(?,?,?,?)""",
            ("example/scores", "https://github.com/example/scores", "main", "2026-11-11"),
        ).lastrowid
        conn.execute(
            """INSERT INTO github_files
               (repository_id,path,name,extension,first_seen_at,last_seen_at,
                repository_license_spdx,license_scope,rights_status,distribution_mode)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (repo_id, "scores/score.mxl", "score.mxl", ".mxl",
             "2026-11-11", "2026-11-11", "CC-BY-4.0",
             "repository", "unverified", "external_link"),
        )
        row = conn.execute("SELECT * FROM github_files").fetchone()
    assert row["repository_license_spdx"] == "CC-BY-4.0"
    assert row["license_spdx"] is None
    assert row["license_scope"] == "repository"
    assert row["distribution_mode"] == "external_link"
