from fastapi.testclient import TestClient

import app.github_catalog as github_catalog
import app.main as main


def test_github_extract_route_persists_text_and_renders_it(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-extraction.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)
    source = tmp_path / "Ave-verum.abc"
    source.write_text("X:1\nT:Ave verum corpus\nK:C\nCDEF", encoding="utf-8")

    with TestClient(main.app) as client:
        github_catalog.ensure_schema()
        with github_catalog.db() as conn:
            repository_id = conn.execute(
                """INSERT INTO github_repositories
                   (full_name,html_url,default_branch,added_at)
                   VALUES(?,?,?,?)""",
                ("test-owner/extract-repo", "https://github.com/test-owner/extract-repo",
                 "main", main.now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO github_files
                   (repository_id,path,name,extension,size_bytes,sha,html_url,first_seen_at,last_seen_at,
                    download_status,local_path,sha256,downloaded_bytes)
                   VALUES(?,?,?,?,?,?,?,?,?,'downloaded',?,?,?)""",
                (repository_id, "Ave-verum.abc", "Ave-verum.abc", ".abc", source.stat().st_size,
                 "git-blob-sha", "https://github.com/test-owner/extract-repo/blob/main/Ave-verum.abc",
                 main.now(), main.now(), str(source), "text-content-sha", source.stat().st_size),
            )

        response = client.post(f"/github/{repository_id}/extract", follow_redirects=False)
        assert response.status_code == 303

        page = client.get(f"/github/{repository_id}")

    assert "text_checked" in page.text
    assert "Ver texto extraído" in page.text
    assert "Ave verum corpus" in page.text
    with github_catalog.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0] == 0
