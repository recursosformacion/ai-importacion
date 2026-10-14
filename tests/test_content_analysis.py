from fastapi.testclient import TestClient
import app.main as main
import app.file_catalog as file_catalog


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
        assert "no se crean ni exportan obras OSAP" in response.text

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


def test_guided_workflow_navigation_links_item_steps(tmp_path, monkeypatch):
    db_path = str(tmp_path / "workflow.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    with TestClient(main.app) as client:
        seed_item()

        detail = client.get("/items/anthology-test")
        assert detail.status_code == 200
        assert "Captación musical · paso 2 de 5" in detail.text
        assert "href='/files/anthology-test'" in detail.text
        assert "Siguiente paso: consultar archivos" in detail.text

        files = client.get("/files/anthology-test")
        assert files.status_code == 200
        assert "Captación musical · paso 3 de 5" in files.text
        assert "href='/items/anthology-test/content-analysis#analisis'" in files.text
        assert "href='/items/anthology-test/content-analysis?step=5#obras'" in files.text

        review = client.get("/items/anthology-test/content-analysis?step=5")
        assert review.status_code == 200
        assert "Captación musical · paso 5 de 5" in review.text
        assert "id='obras'" in review.text


def test_search_results_show_next_workflow_step(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "search-workflow.sqlite3"))

    async def fake_archive_search(archive_query, rows):
        return {
            "total": 1,
            "docs": [{
                "identifier": "motet-test",
                "title": "Motet Test",
                "creator": "Composer",
                "date": "1900",
                "mediatype": "texts",
                "collection": [],
                "rights": "Public domain",
                "description": "A test score",
            }],
        }

    monkeypatch.setattr(main, "archive_search", fake_archive_search)
    with TestClient(main.app) as client:
        response = client.post("/search", data={"query": "motet", "rows": "10", "scope": "all"})

    assert response.status_code == 200
    assert "Captación musical · paso 2 de 5" in response.text
    assert "Elige un resultado para abrir su ficha" in response.text
    assert "href='/items/motet-test'" in response.text


def test_search_error_keeps_guided_navigation_and_retry_action(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "search-error.sqlite3"))

    async def failed_archive_search(archive_query, rows):
        raise RuntimeError("<fallo remoto>")

    monkeypatch.setattr(main, "archive_search", failed_archive_search)
    with TestClient(main.app) as client:
        response = client.post(
            "/search",
            data={"query": "motet", "rows": "10", "scope": "all"},
        )

    assert response.status_code == 200
    assert "Captación musical · paso 1 de 5" in response.text
    assert "No se pudo completar la búsqueda" in response.text
    assert "Volver al buscador y reintentar" in response.text
    assert "&lt;fallo remoto&gt;" in response.text
    assert "<fallo remoto>" not in response.text
    with main.db() as conn:
        run = conn.execute(
            "SELECT status,error_text FROM search_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert run["status"] == "error"
    assert "<fallo remoto>" in run["error_text"]
