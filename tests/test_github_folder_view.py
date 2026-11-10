from fastapi.testclient import TestClient

import app.github_catalog as github_catalog
import app.main as main


def test_repository_view_groups_directories_and_shows_xml_candidates(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-folders.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)

    with TestClient(main.app) as client:
        github_catalog.ensure_schema()
        with github_catalog.db() as conn:
            repository_id = conn.execute(
                "INSERT INTO github_repositories (full_name,html_url,default_branch,added_at) VALUES(?,?,?,?)",
                ("musetrainer/library", "https://github.com/musetrainer/library", "main", main.now()),
            ).lastrowid
            for path, extension in (
                ("scores/Air.mxl", ".mxl"),
                ("scores/Example.xml", ".xml"),
                ("README.txt", ".txt"),
            ):
                conn.execute(
                    "INSERT INTO github_files (repository_id,path,name,extension,html_url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                    (repository_id, path, path.rsplit("/", 1)[-1], extension,
                     "https://github.com/musetrainer/library/blob/main/" + path, main.now(), main.now()),
                )

        response = client.get(f"/github/{repository_id}")

    assert response.status_code == 200
    assert "Carpetas con archivos localizados" in response.text
    assert "scores/Air.mxl" in response.text
    assert "scores/Example.xml" in response.text
    assert "README.txt" not in response.text
