from fastapi.testclient import TestClient
import app.main as main


def seed_item():
    with main.db() as conn:
        stamp = main.now()
        conn.execute(
            """INSERT INTO archive_items
            (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?)""",
            ("anthology-test", "Anthology of scores", "https://archive.org/details/anthology-test", "{}", stamp, stamp),
        )


def test_content_scope_and_candidate_units(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "content.sqlite3"))
    with TestClient(main.app) as client:
        seed_item()
        response = client.get("/items/anthology-test/content-analysis")
        assert response.status_code == 200
        assert "No se crean ni exportan obras OSAP" in response.text

        response = client.post(
            "/items/anthology-test/content-analysis/scope",
            data={"scope": "collection", "rationale": "El título indica un volumen."},
            follow_redirects=False,
        )
        assert response.status_code == 303

        for title in ("Motet A", "Motet B"):
            response = client.post(
                "/items/anthology-test/content-analysis/units",
                data={"title": title, "creator": "Composer", "source_file": "scores.pdf",
                      "page_range": "1-4", "evidence": "Índice del PDF", "notes": ""},
                follow_redirects=False,
            )
            assert response.status_code == 303

        with main.db() as conn:
            assessment = conn.execute(
                "SELECT scope FROM archive_content_assessments WHERE identifier=?",
                ("anthology-test",),
            ).fetchone()
            units = conn.execute(
                "SELECT title,ordinal FROM archive_item_contents WHERE identifier=? ORDER BY ordinal",
                ("anthology-test",),
            ).fetchall()
        assert assessment["scope"] == "collection"  # No sobrescribir una decisión explícita del usuario.
        assert [(row["title"], row["ordinal"]) for row in units] == [("Motet A", 1), ("Motet B", 2)]


def test_two_candidates_suggest_multiple_works_when_unclassified(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "suggest.sqlite3"))
    with TestClient(main.app) as client:
        seed_item()
        for title in ("Piece A", "Piece B"):
            client.post("/items/anthology-test/content-analysis/units", data={"title": title})
        with main.db() as conn:
            row = conn.execute(
                "SELECT scope,rationale FROM archive_content_assessments WHERE identifier=?",
                ("anthology-test",),
            ).fetchone()
        assert row["scope"] == "multiple_works"
        assert "requiere revisión humana" in row["rationale"]
