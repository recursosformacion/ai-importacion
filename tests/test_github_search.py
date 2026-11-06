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
        if "/git/trees/" in url:
            return {"tree": [
                {"type": "blob", "path": f"scores/piece-{i}.musicxml"} for i in range(12)
            ] + [
                {"type": "blob", "path": "README.txt"},
                {"type": "blob", "path": "metadata.xml"},
            ]}
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



def test_github_search_without_query_runs_automatic_format_discovery(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-auto-search.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)
    requested = []

    async def fake_github_json(url):
        from urllib.parse import parse_qs, urlparse
        if "/git/trees/" in url:
            return {"tree": [
                {"type": "blob", "path": f"score-{i}.musicxml"} for i in range(12)
            ] + [
                {"type": "blob", "path": "notes.txt"},
                {"type": "blob", "path": "config.xml"},
            ]}
        term = parse_qs(urlparse(url).query)["q"][0]
        requested.append(term)
        return {
            "total_count": 1,
            "items": [{
                "full_name": "OpenMusic/All-Formats",
                "html_url": "https://github.com/OpenMusic/All-Formats",
                "description": "Repository containing varied notation formats",
                "language": "Python",
                "stargazers_count": 5,
                "updated_at": "2026-11-05T12:00:00Z",
                "license": None,
            }],
        }

    monkeypatch.setattr(github_catalog, "github_json", fake_github_json)

    with TestClient(main.app) as client:
        response = client.get("/github/search")

    assert response.status_code == 200
    assert len(requested) == 7
    assert any("musicxml" in term for term in requested)
    assert any("midi" in term for term in requested)
    assert any("lilypond" in term for term in requested)
    assert "OpenMusic/All-Formats" in response.text
    assert "Búsqueda automática" in response.text
    assert "12" in response.text
    assert "al menos 11 archivos musicales" in response.text
    assert response.text.count("OpenMusic/All-Formats") == 1



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



def test_github_search_excludes_repositories_with_ten_or_fewer_musical_files(tmp_path, monkeypatch):
    db_path = str(tmp_path / "github-threshold.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(github_catalog, "DB_PATH", db_path)

    async def fake_github_json(url):
        if "/git/trees/" in url:
            return {"tree": [
                {"type": "blob", "path": f"piece-{i}.musicxml"} for i in range(10)
            ] + [
                {"type": "blob", "path": f"test-{i}.txt"} for i in range(100)
            ] + [
                {"type": "blob", "path": f"config-{i}.xml"} for i in range(100)
            ]}
        return {
            "total_count": 1,
            "items": [{
                "full_name": "Example/TestOnly",
                "default_branch": "main",
                "html_url": "https://github.com/Example/TestOnly",
                "description": "Test fixtures",
                "language": "Python",
                "stargazers_count": 1,
                "updated_at": "2026-11-05T12:00:00Z",
                "license": None,
            }],
        }

    monkeypatch.setattr(github_catalog, "github_json", fake_github_json)
    with TestClient(main.app) as client:
        response = client.get("/github/search", params={"q": "music"})
    assert response.status_code == 200
    assert "Example/TestOnly" not in response.text
    assert "No se han encontrado repositorios" in response.text
