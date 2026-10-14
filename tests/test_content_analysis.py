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


def test_inventory_error_keeps_recovery_link_and_escapes_detail(tmp_path, monkeypatch):
    db_path = str(tmp_path / "inventory-error.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)

    class FailedResponse:
        def raise_for_status(self):
            raise RuntimeError("<red no disponible>")

    class FailedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FailedResponse()

    monkeypatch.setattr(file_catalog.httpx, "AsyncClient", FailedClient)
    with TestClient(main.app) as client:
        seed_item()
        response = client.post("/items/anthology-test/inspect-files")

    assert response.status_code == 502
    assert "Captación musical · paso 3 de 5" in response.text
    assert "No se pudo actualizar el inventario" in response.text
    assert "Volver al inventario y reintentar" in response.text
    assert "&lt;red no disponible&gt;" in response.text
    assert "<red no disponible>" not in response.text


def test_file_inventory_localizes_download_states_and_avoids_oversized_retry(tmp_path, monkeypatch):
    db_path = str(tmp_path / "download-states.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            conn.execute(
                "INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status) VALUES(?,?,?,?)",
                ("anthology-test", stamp, 2, "ok"),
            )
            common = (
                "anthology-test", "score.pdf", "score.pdf",
                "https://archive.org/download/anthology-test/score.pdf",
                1024, "PDF", "abc", "def", 1, stamp,
            )
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,md5,sha1,relevant,inventory_seen_at,download_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (*common, "too_large"),
            )
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,md5,sha1,relevant,inventory_seen_at,download_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("anthology-test", "editable.mxl", "editable.mxl",
                 "https://archive.org/download/anthology-test/editable.mxl",
                 512, "MusicXML", None, None, 1, stamp, "not_downloaded"),
            )

        response = client.get("/files/anthology-test")

    assert response.status_code == 200
    assert "No descargado: supera el límite configurado" in response.text
    assert "Pendiente de descarga" in response.text
    assert "No disponible por el límite de tamaño" in response.text
    # The oversized row has no retry form; only the pending MXL row has a download action.
    assert response.text.count("<button>Descargar</button>") == 1
    assert "download_status" not in response.text


def test_downloads_with_same_sanitized_name_do_not_overwrite_each_other(tmp_path, monkeypatch):
    db_path = str(tmp_path / "filename-collision.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DOWNLOAD_DIR", tmp_path / "downloads")

    class FakeResponse:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, chunk_size):
            yield b"music score content"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(file_catalog.httpx, "AsyncClient", FakeClient)
    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            conn.execute(
                "INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status) VALUES(?,?,?,?)",
                ("anthology-test", stamp, 2, "ok"),
            )
            for name in ("score:final.pdf", "score_final.pdf"):
                conn.execute(
                    """INSERT INTO archive_files
                    (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,relevant,inventory_seen_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    ("anthology-test", name, file_catalog.safe_filename(name),
                     "https://archive.org/download/anthology-test/" + name, 100, "PDF", 1, stamp),
                )
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM archive_files WHERE identifier=? ORDER BY file_name",
                ("anthology-test",),
            ).fetchall()]

        for file_id in ids:
            response = client.post(f"/files/download/{file_id}", follow_redirects=False)
            assert response.status_code == 303

        with main.db() as conn:
            rows = conn.execute(
                "SELECT file_name,local_path,download_status FROM archive_files WHERE identifier=? ORDER BY file_name",
                ("anthology-test",),
            ).fetchall()
        assert all(row["download_status"] == "downloaded" for row in rows)
        assert rows[0]["local_path"] != rows[1]["local_path"]
        paths = [__import__("pathlib").Path(row["local_path"]) for row in rows]
        assert all(path.read_bytes() == b"music score content" for path in paths)



def test_integrated_intake_links_resume_inventory_and_analysis_safely():
    links = file_catalog._intake_item_links([
        ("score/edition", "<Score & edition>")
    ])

    assert "href='/files/score%2Fedition'" in links
    assert "href='/items/score%2Fedition/content-analysis'" in links
    assert "&lt;Score &amp; edition&gt;" in links
    assert "<Score & edition>" not in links



def test_integrated_intake_caps_failed_download_attempts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "intake-attempt-limit.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    attempts = []

    async def failed_download(file_id):
        attempts.append(file_id)
        with file_catalog.db() as conn:
            conn.execute(
                "UPDATE archive_files SET download_status='error',download_error=? WHERE id=?",
                ("red no disponible", file_id),
            )

    monkeypatch.setattr(file_catalog, "download_file", failed_download)
    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            conn.execute(
                "INSERT INTO global_discovery_seen(identifier,first_query_key,score,reason,seen_at) VALUES(?,?,?,?,?)",
                ("anthology-test", "test-query", 100, "music score", stamp),
            )
            conn.execute(
                "INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status) VALUES(?,?,?,?)",
                ("anthology-test", stamp, 2, "ok"),
            )
            for name in ("first-score.pdf", "second-score.pdf"):
                conn.execute(
                    """INSERT INTO archive_files
                    (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,relevant,inventory_seen_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    ("anthology-test", name, name,
                     "https://archive.org/download/anthology-test/" + name, 100, "PDF", 1, stamp),
                )

        response = client.post("/intake/run", data={"items": "1", "downloads": "1"})
        assert response.status_code == 200
        assert len(attempts) == 1
        assert "Intentos de descarga realizados: <b>1</b> de un máximo de 1" in response.text
        assert "descargas correctas: <b>0</b>" in response.text
        with main.db() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT download_status FROM archive_files ORDER BY id"
            ).fetchall()]
        assert statuses.count("error") == 1
        assert statuses.count("not_downloaded") == 1



def test_integrated_intake_error_links_escape_details_and_return_to_item():
    rendered = file_catalog._intake_error_link(
        "score/edition",
        "<score>.pdf",
        "HTTP error: <unavailable & retry>",
    )

    assert "href='/files/score%2Fedition'" in rendered
    assert "&lt;score&gt;.pdf" in rendered
    assert "HTTP error: &lt;unavailable &amp; retry&gt;" in rendered
    assert "<unavailable & retry>" not in rendered



def test_global_discovery_resumes_each_query_from_saved_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "discovery-resume.sqlite3"))
    calls = []

    async def fake_archive_scrape(query, count, cursor=None, query_key="default"):
        calls.append((query_key, cursor))
        if cursor is None:
            return {
                "total": 2,
                "items": [{
                    "identifier": "sheet-music-first-" + query_key,
                    "title": "Sheet music score first " + query_key,
                    "description": "Musical score",
                }],
                "cursor": "next-" + query_key,
            }
        return {
            "total": 2,
            "items": [{
                "identifier": "sheet-music-second-" + query_key,
                "title": "Sheet music score second " + query_key,
                "description": "Musical score",
            }],
            "cursor": None,
        }

    monkeypatch.setattr(main, "archive_scrape", fake_archive_scrape)
    with TestClient(main.app) as client:
        first = client.post("/discover", data={"rows": "100"})
        assert first.status_code == 200
        assert "Continuar con el siguiente lote" in first.text
        assert len(calls) == len(main.GLOBAL_DISCOVERY_QUERIES)
        with main.db() as conn:
            first_states = conn.execute(
                "SELECT query_key,cursor,status,scanned_count FROM global_discovery_queries"
            ).fetchall()
        assert all(row["cursor"] == "next-" + row["query_key"] for row in first_states)
        assert all(row["status"] == "active" and row["scanned_count"] == 1 for row in first_states)

        second = client.post("/discover", data={"rows": "100"})
        assert second.status_code == 200
        assert "han llegado al final de sus cursores" in second.text

    expected_keys = {key for key, _, _ in main.GLOBAL_DISCOVERY_QUERIES}
    assert {(key, cursor) for key, cursor in calls[:len(expected_keys)]} == {
        (key, None) for key in expected_keys
    }
    assert {(key, cursor) for key, cursor in calls[len(expected_keys):]} == {
        (key, "next-" + key) for key in expected_keys
    }
    with main.db() as conn:
        final_states = conn.execute(
            "SELECT query_key,cursor,status,scanned_count FROM global_discovery_queries"
        ).fetchall()
        unique_seen = conn.execute("SELECT COUNT(*) FROM global_discovery_seen").fetchone()[0]
    assert all(row["cursor"] is None for row in final_states)
    assert all(row["status"] == "completed" and row["scanned_count"] == 2 for row in final_states)
    assert unique_seen == len(expected_keys) * 2
