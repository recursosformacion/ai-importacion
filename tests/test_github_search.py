from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

import app.github_catalog as github_catalog
import app.main as main


def test_github_repository_search_renders_results_and_add_action(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-search.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)
    requested = {}

    async def fake_github_json(url):
        requested["url"] = url
        return {
            "total_count": 1,
            "items": [{
                "full_name": "OpenScore/Lieder",
                "html_url": "https://github.com/OpenScore/Lieder",
                "description": "Open music scores and vocal parts",
                "language": "Python",
                "stargazers_count": 42,
                "updated_at": "2026-11-05T12:00:00Z",
                "license": {"spdx_id": "CC-BY-4.0"},
            }],
        }

    monkeypatch.setattr(github_catalog, "github_json", fake_github_json)

    with TestClient(main.app) as client:
        response = client.get(
            "/github/search",
            params={"q": "choral musicxml", "sort": "stars", "license": "cc-by-4.0"},
        )

    assert response.status_code == 200
    assert "OpenScore/Lieder" in response.text
    assert "Open music scores and vocal parts" in response.text
    assert "action='/github/add'" in response.text
    assert "value='OpenScore/Lieder'" in response.text
    query = parse_qs(urlparse(requested["url"]).query)["q"][0]
    assert "choral musicxml" in query
    assert "license:cc-by-4.0" in query
    assert "fork:false" in query
