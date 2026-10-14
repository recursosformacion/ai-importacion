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
        assert "Captación musical · paso 2 de 5" not in detail.text
        assert "href='/files/anthology-test'" in detail.text
        assert "Consultar archivos →" in detail.text

        files = client.get("/files/anthology-test")
        assert files.status_code == 200
        assert "Captación musical · paso 3 de 5" not in files.text
        assert "href='/items/anthology-test/content-analysis'" in files.text
        assert "Siguiente: revisar obras" not in files.text

        review = client.get("/items/anthology-test/content-analysis?step=5")
        assert review.status_code == 200
        assert "Captación musical · paso 5 de 5" not in review.text
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
    assert "Captación musical · paso 2 de 5" not in response.text
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
    assert "Captación musical · paso 1 de 5" not in response.text
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
    assert "Captación musical · paso 3 de 5" not in response.text
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


def test_global_discovery_retries_failed_query_without_losing_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "discovery-retry.sqlite3"))
    calls = []
    failed_once = set()
    failing_key = main.GLOBAL_DISCOVERY_QUERIES[0][0]

    async def flaky_archive_scrape(query, count, cursor=None, query_key="default"):
        calls.append((query_key, cursor))
        if query_key == failing_key and query_key not in failed_once:
            failed_once.add(query_key)
            raise RuntimeError("temporary archive outage")
        next_cursor = "resume-" + query_key if cursor is None else None
        return {
            "total": 1,
            "items": [{
                "identifier": "sheet-music-retry-" + query_key,
                "title": "Sheet music score " + query_key,
                "description": "Musical score",
            }],
            "cursor": next_cursor,
        }

    monkeypatch.setattr(main, "archive_scrape", flaky_archive_scrape)
    with TestClient(main.app) as client:
        first = client.post("/discover", data={"rows": "100"})
        assert first.status_code == 200
        assert "temporary archive outage" in first.text
        with main.db() as conn:
            run_state = conn.execute(
                "SELECT status,error_text FROM search_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert run_state["status"] == "partial"
            assert "temporary archive outage" in run_state["error_text"]
            state = conn.execute(
                "SELECT cursor,status,scanned_count FROM global_discovery_queries WHERE query_key=?",
                (failing_key,),
            ).fetchone()
        assert state["cursor"] is None
        assert state["status"] == "error"
        assert state["scanned_count"] == 0

        second = client.post("/discover", data={"rows": "100"})
        assert second.status_code == 200
        assert (failing_key, None) in calls
        with main.db() as conn:
            state = conn.execute(
                "SELECT cursor,status,scanned_count FROM global_discovery_queries WHERE query_key=?",
                (failing_key,),
            ).fetchone()
        assert state["cursor"] == "resume-" + failing_key
        assert state["status"] == "active"
        assert state["scanned_count"] == 1



def test_musicxml_analysis_retries_failures_and_skips_unchanged_success(tmp_path, monkeypatch):
    db_path = str(tmp_path / "analysis-cache.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    score_path = tmp_path / "score.musicxml"
    score_path.write_text("<score-partwise/>", encoding="utf-8")
    calls = []

    def flaky_analysis(path):
        calls.append(str(path))
        if len(calls) == 1:
            raise ValueError("temporary malformed file")
        return {"title": "Test score", "part_count": 1, "measure_count": 8}

    monkeypatch.setattr(main, "analyze_musicxml", flaky_analysis)
    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,relevant,inventory_seen_at,
                 download_status,local_path,sha256)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                ("anthology-test", "score.musicxml", "score.musicxml",
                 "https://archive.org/download/anthology-test/score.musicxml", 1,
                 main.now(), "downloaded", str(score_path), "digest-v1"),
            )

        first = client.post(
            "/items/anthology-test/content-analysis/analyze-files",
            follow_redirects=False,
        )
        assert first.status_code == 303
        assert "analizados=1" in first.headers["location"]
        assert "fallidos=1" in first.headers["location"]
        with main.db() as conn:
            result = conn.execute(
                "SELECT status,error_text,sha256 FROM archive_file_analyses"
            ).fetchone()
        assert result["status"] == "error"
        assert "temporary malformed file" in result["error_text"]

        second = client.post(
            "/items/anthology-test/content-analysis/analyze-files",
            follow_redirects=False,
        )
        assert second.status_code == 303
        assert "analizados=1" in second.headers["location"]
        assert "fallidos=0" in second.headers["location"]
        with main.db() as conn:
            result = conn.execute(
                "SELECT status,error_text,sha256 FROM archive_file_analyses"
            ).fetchone()
        assert result["status"] == "ok"
        assert result["error_text"] is None
        assert result["sha256"] == "digest-v1"

        third = client.post(
            "/items/anthology-test/content-analysis/analyze-files",
            follow_redirects=False,
        )
        assert third.status_code == 303
        assert "analizados=0" in third.headers["location"]
        assert "omitidos=1" in third.headers["location"]
        assert "fallidos=0" in third.headers["location"]
        assert len(calls) == 2

def test_pdf_extraction_reports_text_ocr_and_read_errors(tmp_path, monkeypatch):
    import pypdf
    import pytesseract
    import zipfile

    monkeypatch.setattr(
        pytesseract,
        "get_tesseract_version",
        lambda: (_ for _ in ()).throw(RuntimeError("Tesseract unavailable in test")),
    )

    db_path = str(tmp_path / "pdf-extraction.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    seed_files = [
        ("scores-text.pdf", "scores-text.pdf"),
        ("scores-scan.pdf", "scores-scan.pdf"),
        ("archive.ocr", "archive.ocr"),
        ("score.abc", "score.abc"),
        ("score.ly", "score.ly"),
        ("score.mei", "score.mei"),
        ("score.mscx", "score.mscx"),
        ("score.mscz", "score.mscz"),
        ("score.sib", "score.sib"),
        ("broken.pdf", "broken.pdf"),
    ]
    for _, name in seed_files:
        (tmp_path / name).write_bytes(b"fixture")

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        def __init__(self, path):
            if str(path).endswith("broken.pdf"):
                raise ValueError("invalid PDF fixture")
            if str(path).endswith("scores-text.pdf"):
                self.pages = [Page("Sonata for Violin and Piano")]
            elif str(path).endswith("scores-scan.pdf"):
                self.pages = [Page("")]
            else:
                raise AssertionError("TXT/OCR files should not use PdfReader")

    monkeypatch.setattr(pypdf, "PdfReader", Reader)

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            for name, local_name in seed_files:
                conn.execute(
                    """INSERT INTO archive_files
                    (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path)
                    VALUES(?,?,?,?,?,'downloaded',?)""",
                    ("anthology-test", name, local_name, "https://archive.org/download/anthology-test/" + name,
                     stamp, str(tmp_path / local_name)),
                )
        (tmp_path / "archive.ocr").write_text("Mass in G minor", encoding="utf-8")
        (tmp_path / "score.abc").write_text("X:1\nT:Chorale in F\nC:Someone\nK:F\n", encoding="utf-8")
        (tmp_path / "score.ly").write_text('\\header {\n  title = "Prelude in C"\n}\n', encoding="utf-8")
        (tmp_path / "score.mei").write_text(
            "<mei><meiHead><fileDesc><titleStmt><title>Motet in D</title></titleStmt></fileDesc></mei>",
            encoding="utf-8",
        )
        (tmp_path / "score.mscx").write_text(
            '<museScore><metaTag name="workTitle">String Quartet in A</metaTag></museScore>',
            encoding="utf-8",
        )
        with zipfile.ZipFile(tmp_path / "score.mscz", "w") as archive:
            archive.writestr(
                "score.mscx",
                '<museScore><metaTag name="workTitle">Symphony in E</metaTag></museScore>',
            )

        response = client.post("/items/anthology-test/content-analysis/extract", follow_redirects=False)
        assert response.status_code == 303
        assert "pdf_con_texto=1" in response.headers["location"]
        assert "pdf_ocr=1" in response.headers["location"]
        assert "txt_leidos=1" in response.headers["location"]
        assert "errores_extraccion=1" in response.headers["location"]
        assert "notacion_leida=5" in response.headers["location"]
        assert "sugerencias_nuevas=7" in response.headers["location"]

        page = client.get(response.headers["location"])
        assert "PDF con texto extraíble: 1" in page.text
        assert "PDF que aún necesitan OCR: 1" in page.text
        assert "Tesseract no está instalado" in page.text
        assert "Estado de lectura de documentos e imágenes" in page.text
        assert "scores-text.pdf" in page.text
        assert "scores-scan.pdf" in page.text
        assert "OCR no disponible" in page.text
        assert "Archivos ABC/LilyPond/MEI/MuseScore leídos: 5" in page.text
        assert "Otros formatos descargados, pendientes de análisis automático" in page.text
        assert "score.sib" in page.text
        assert "todavía no extrae sus metadatos" in page.text

    with main.db() as conn:
        suggestions = conn.execute(
            "SELECT suggestion_text FROM archive_content_suggestions ORDER BY suggestion_text"
        ).fetchall()
        pdf_results = conn.execute(
            """SELECT f.file_name,e.status,e.page_count,e.extracted_characters
            FROM archive_pdf_extractions e JOIN archive_files f ON f.id=e.file_id
            ORDER BY f.file_name"""
        ).fetchall()
    assert [row["suggestion_text"] for row in suggestions] == [
        "Chorale in F", "Mass in G minor", "Motet in D", "Prelude in C", "Sonata for Violin and Piano",
        "String Quartet in A", "Symphony in E"
    ]
    statuses = {row["file_name"]: row["status"] for row in pdf_results}
    assert statuses == {
        "broken.pdf": "error",
        "scores-scan.pdf": "ocr_unavailable",
        "scores-text.pdf": "text_available",
    }
    assert {row["file_name"]: row["page_count"] for row in pdf_results} == {
        "broken.pdf": 0, "scores-scan.pdf": 1, "scores-text.pdf": 1,
    }

def test_scanned_pdf_ocr_extracts_text_and_creates_suggestion(tmp_path, monkeypatch):
    import io
    import fitz
    import pypdf
    import pytesseract
    from PIL import Image

    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "ocr-success.sqlite3"))
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"mock PDF")

    class Page:
        def extract_text(self):
            return ""

    class Reader:
        pages = [Page()]

        def __init__(self, path):
            pass

    class Pixmap:
        def tobytes(self, image_format):
            buffer = io.BytesIO()
            Image.new("RGB", (16, 16), "white").save(buffer, format="PNG")
            return buffer.getvalue()

    class Rect:
        width = 612
        height = 792

    class RasterPage:
        rect = Rect()

        def get_pixmap(self, matrix, alpha):
            assert matrix.a == 2
            assert alpha is False
            return Pixmap()

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def __getitem__(self, index):
            assert index == 0
            return RasterPage()

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.0")
    monkeypatch.setenv("AI_IMPORTACION_OCR_LANG", "eng+spa")

    def recognize(image, lang):
        assert lang == "eng+spa"
        return "Prelude in C"

    monkeypatch.setattr(pytesseract, "image_to_string", recognize)
    monkeypatch.setattr(fitz, "open", lambda path: Document())

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path)
                VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "scan.pdf", "scan.pdf",
                 "https://archive.org/download/anthology-test/scan.pdf",
                 main.now(), str(pdf_path)),
            )

        response = client.post(
            "/items/anthology-test/content-analysis/extract", follow_redirects=False
        )
        assert response.status_code == 303
        assert "pdf_ocr_realizados=1" in response.headers["location"]

    with main.db() as conn:
        result = conn.execute(
            """SELECT status,extracted_characters FROM archive_pdf_extractions
            WHERE identifier='anthology-test'"""
        ).fetchone()
        suggestions = conn.execute(
            "SELECT suggestion_text FROM archive_content_suggestions"
        ).fetchall()
    assert result["status"] == "ocr_completed"
    assert result["extracted_characters"] == len("Prelude in C")
    assert [row["suggestion_text"] for row in suggestions] == ["Prelude in C"]



def test_capx_title_candidates_only_use_explicit_xml_metadata():
    from app.main import extract_notation_title_candidates

    xml = """<?xml version="1.0"?>\n<score><metaTag name="workTitle">A Capella Work</metaTag><title>Motet in G</title></score>"""
    candidates = extract_notation_title_candidates(xml, ".capx")
    assert [title for _, title in candidates] == ["A Capella Work", "Motet in G"]
    assert extract_notation_title_candidates("<score><composer>Unknown</composer></score>", ".capx") == []



def test_scanned_score_image_ocr_extracts_possible_title(tmp_path, monkeypatch):
    from PIL import Image
    import pytesseract

    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "image-ocr.sqlite3"))
    image_path = tmp_path / "score.png"
    Image.new("RGB", (80, 80), "white").save(image_path)
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.0")

    def recognize(image, lang):
        assert image.size == (80, 80)
        assert lang == "eng+spa"
        return "Prelude in C"

    monkeypatch.setattr(pytesseract, "image_to_string", recognize)
    monkeypatch.setenv("AI_IMPORTACION_OCR_LANG", "eng+spa")

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path,sha256)
                VALUES(?,?,?,?,?,'downloaded',?,?)""",
                ("anthology-test", "score.png", "score.png",
                 "https://archive.org/download/anthology-test/score.png",
                 main.now(), str(image_path), "image-sha"),
            )
        response = client.post(
            "/items/anthology-test/content-analysis/extract", follow_redirects=False
        )
        assert response.status_code == 303
        assert "imagenes_ocr=1" in response.headers["location"]

    with main.db() as conn:
        result = conn.execute(
            "SELECT status,extracted_characters FROM archive_pdf_extractions WHERE identifier='anthology-test'"
        ).fetchone()
        suggestions = conn.execute(
            "SELECT suggestion_text FROM archive_content_suggestions WHERE identifier='anthology-test'"
        ).fetchall()
    assert result["status"] == "image_ocr_completed"
    assert result["extracted_characters"] == len("Prelude in C")
    assert [row["suggestion_text"] for row in suggestions] == ["Prelude in C"]


def test_ocr_candidate_filter_drops_obvious_noise_and_credit_lines():
    from app.main import extract_text_candidates

    text = """SPIRIT OF THE AIR FORCE
aS SS
= cs S|
Ae sos Shea
Words and Music by
Col. WILLIAM J. CLINCH
Official Song of the Cadet Battalion
my Give ‘er the
and our steel props he to whine you say
MEXICO
THE THREE CABALLEROS
"""
    candidates = [title for _, title in extract_text_candidates(text)]
    assert "SPIRIT OF THE AIR FORCE" in candidates
    assert "Official Song of the Cadet Battalion" in candidates
    assert "Col. WILLIAM J. CLINCH" not in candidates
    assert "MEXICO" in candidates
    assert "THE THREE CABALLEROS" in candidates
    assert "aS SS" not in candidates
    assert "= cs S|" not in candidates
    assert "Ae sos Shea" not in candidates
    assert "Words and Music by" not in candidates
    assert "my Give ‘er the" not in candidates
    assert "and our steel props he to whine you say" not in candidates


def test_capx_files_are_included_in_local_content_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "capx-extract.sqlite3"))
    capx_path = tmp_path / "score.capx"
    with __import__("zipfile").ZipFile(capx_path, "w") as archive:
        archive.writestr(
            "score.xml",
            '<score><title>Motet in G</title></score>',
        )

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path)
                VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "score.capx", "score.capx",
                 "https://archive.org/download/anthology-test/score.capx",
                 main.now(), str(capx_path)),
            )
        response = client.post(
            "/items/anthology-test/content-analysis/extract", follow_redirects=False
        )
        assert response.status_code == 303
        assert "notacion_leida=1" in response.headers["location"]

    with main.db() as conn:
        suggestions = conn.execute(
            "SELECT suggestion_text FROM archive_content_suggestions"
        ).fetchall()
    assert [row["suggestion_text"] for row in suggestions] == ["Motet in G"]



def test_batch_processing_analyzes_downloaded_items_without_per_file_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "batch-process.sqlite3"))
    calls = []

    def fake_extract(identifier):
        calls.append(identifier)
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_content_suggestions
                (identifier,source_file,line_number,suggestion_text,status,created_at)
                VALUES(?,?,?,?, 'pending', ?)""",
                (identifier, "score.txt", 1, "Prelude in C", main.now()),
            )

    monkeypatch.setattr(main, "extract_content_suggestions", fake_extract)

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path)
                VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "score.txt", "score.txt",
                 "https://archive.org/download/anthology-test/score.txt",
                 main.now(), str(tmp_path / "score.txt")),
            )
        response = client.post("/intake/process")
        assert response.status_code == 200
        assert "Resultado del análisis automático" in response.text
        assert "Anthology of scores" in response.text
        assert "1</td>" in response.text
        assert calls == ["anthology-test"]

    with main.db() as conn:
        row = conn.execute(
            "SELECT suggestion_text FROM archive_content_suggestions WHERE identifier='anthology-test'"
        ).fetchone()
    assert row["suggestion_text"] == "Prelude in C"



def test_midi_analysis_records_tracks_instruments_and_timing(tmp_path, monkeypatch):
    import json
    import mido

    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "midi-analysis.sqlite3"))
    midi_path = tmp_path / "example.mid"
    midi = mido.MidiFile()
    meta_track = mido.MidiTrack()
    midi.tracks.append(meta_track)
    meta_track.extend([
        mido.MetaMessage("track_name", name="Example piece", time=0),
        mido.MetaMessage("set_tempo", tempo=500000, time=0),
        mido.MetaMessage("time_signature", numerator=3, denominator=4, time=0),
        mido.MetaMessage("key_signature", key="G", time=0),
        mido.MetaMessage("end_of_track", time=0),
    ])
    music_track = mido.MidiTrack()
    midi.tracks.append(music_track)
    music_track.extend([
        mido.Message("program_change", channel=0, program=40, time=0),
        mido.Message("note_on", channel=0, note=67, velocity=80, time=0),
        mido.Message("note_off", channel=0, note=67, velocity=0, time=480),
        mido.MetaMessage("end_of_track", time=0),
    ])
    midi.save(midi_path)

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path,sha256)
                VALUES(?,?,?,?,?,'downloaded',?,?)""",
                ("anthology-test", "example.mid", "example.mid",
                 "https://archive.org/download/anthology-test/example.mid",
                 main.now(), str(midi_path), "test-midi-sha256"),
            )
        response = client.post(
            "/items/anthology-test/content-analysis/analyze-files", follow_redirects=False
        )
        assert response.status_code == 303
        assert "analizados=1" in response.headers["location"]

    with main.db() as conn:
        row = conn.execute(
            "SELECT analysis_type,status,analysis_json FROM archive_file_analyses WHERE identifier=?",
            ("anthology-test",),
        ).fetchone()
    result = json.loads(row["analysis_json"])
    assert row["analysis_type"] == "midi"
    assert row["status"] == "ok"
    assert result["track_count"] == 2
    assert result["track_names"] == ["Example piece"]
    assert result["note_on_count"] == 1
    assert result["time_signatures"] == [{"numerator": 3, "denominator": 4}]
    assert result["key_signatures"] == ["G"]
    assert result["instruments"][0]["program"] == 40
    assert result["duration_seconds"] == 0.5



def test_integrated_intake_runs_automatic_analysis_after_successful_download(tmp_path, monkeypatch):
    db_path = str(tmp_path / "intake-auto-analysis.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    analyzed = []
    local_pdf = tmp_path / "downloaded.pdf"
    local_pdf.write_bytes(b"fixture PDF")

    async def successful_download(file_id):
        with file_catalog.db() as conn:
            conn.execute(
                "UPDATE archive_files SET download_status='downloaded',local_path=?,sha256=? WHERE id=?",
                (str(local_pdf), "sha-fixture", file_id),
            )

    def fake_extract(identifier):
        analyzed.append(identifier)

    monkeypatch.setattr(file_catalog, "download_file", successful_download)
    monkeypatch.setattr(main, "extract_content_suggestions", fake_extract)

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
                ("anthology-test", stamp, 1, "ok"),
            )
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,relevant,inventory_seen_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("anthology-test", "score.pdf", "score.pdf",
                 "https://archive.org/download/anthology-test/score.pdf", 100, "PDF", 1, stamp),
            )

        response = client.post("/intake/run", data={"items": "1", "downloads": "1"})
        assert response.status_code == 200
        assert "Resultado del análisis automático" in response.text
        assert "Inventarios nuevos correctos" in response.text
        assert analyzed == ["anthology-test"]



def test_explicit_notation_title_is_registered_as_candidate_automatically(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "auto-title.sqlite3"))
    score_path = tmp_path / "score.abc"
    score_path.write_text("X:1\nT:Chorale in F\nC:Composer Name\nK:F\n", encoding="utf-8")

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path)
                VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "score.abc", "score.abc",
                 "https://archive.org/download/anthology-test/score.abc",
                 main.now(), str(score_path)),
            )
        response = client.post(
            "/items/anthology-test/content-analysis/extract", follow_redirects=False
        )
        assert response.status_code == 303

    with main.db() as conn:
        unit = conn.execute(
            "SELECT title,source_file,evidence,status FROM archive_item_contents WHERE identifier=?",
            ("anthology-test",),
        ).fetchone()
        suggestion = conn.execute(
            "SELECT status FROM archive_content_suggestions WHERE identifier=?",
            ("anthology-test",),
        ).fetchone()
    assert unit["title"] == "Chorale in F"
    assert unit["source_file"] == "score.abc"
    assert unit["status"] == "candidate"
    assert "metadatos del formato .abc" in unit["evidence"]
    assert suggestion["status"] == "auto_accepted"



def test_reanalysis_hides_stale_ocr_suggestions_without_deleting_history(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "refresh-suggestions.sqlite3"))
    text_path = tmp_path / "archive.ocr"
    text_path.write_text("SPIRIT OF THE AIR FORCE\n", encoding="utf-8")

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            file_id = conn.execute(
                """INSERT INTO archive_files
                (identifier,file_name,file_name_safe,source_url,inventory_seen_at,download_status,local_path)
                VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "archive.ocr", "archive.ocr",
                 "https://archive.org/download/anthology-test/archive.ocr",
                 stamp, str(text_path)),
            ).lastrowid
            conn.execute(
                """INSERT INTO archive_content_suggestions
                (identifier,source_file,line_number,suggestion_text,status,created_at)
                VALUES(?,?,?,?, 'pending', ?)""",
                ("anthology-test", "archive.ocr", 1, "aS SS", stamp),
            )

        response = client.post(
            "/items/anthology-test/content-analysis/extract", follow_redirects=False
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"])
        assert "SPIRIT OF THE AIR FORCE" in page.text
        assert "aS SS" not in page.text

    with main.db() as conn:
        statuses = {
            row["suggestion_text"]: row["status"]
            for row in conn.execute(
                "SELECT suggestion_text,status FROM archive_content_suggestions WHERE identifier=?",
                ("anthology-test",),
            ).fetchall()
        }
    assert statuses["aS SS"] == "filtered_noise"
    assert statuses["SPIRIT OF THE AIR FORCE"] == "pending"



def test_homepage_is_a_single_capture_workbench(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "capture-workbench.sqlite3"))
    with TestClient(main.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Continuar captación automática" in response.text
    assert "Captación musical · paso" not in response.text
    assert "Procesa el siguiente lote de búsquedas, inventarios, descargas y análisis" in response.text


def test_capture_pipeline_route_runs_discovery_then_integrated_intake(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "capture-pipeline.sqlite3"))
    calls = []

    async def fake_discover(rows=100):
        calls.append(("discover", rows))
        return HTMLResponse("discovery complete")

    async def fake_intake(items=10, downloads=3):
        calls.append(("intake", items, downloads))
        return HTMLResponse("intake complete")

    import app.file_catalog as file_catalog
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(file_catalog, "run_integrated_intake", fake_intake)

    with TestClient(main.app) as client:
        response = client.post("/capture/run")
    assert response.status_code == 200
    assert calls == [("discover", 100), ("intake", 10, 3)]
    assert "Lote de captación terminado" in response.text



def test_review_queue_shows_uncertain_item_files_and_evidence(tmp_path, monkeypatch):
    db_path = str(tmp_path / "review-queue.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    download_dir = tmp_path / "downloads"
    monkeypatch.setattr(file_catalog, "DOWNLOAD_DIR", download_dir)
    local_dir = download_dir / "anthology-test"
    local_dir.mkdir(parents=True)
    pdf = local_dir / "score.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            conn.execute(
                """INSERT INTO global_discovery_seen
                   (identifier,first_query_key,score,reason,seen_at)
                   VALUES(?,?,?,?,?)""",
                ("anthology-test", "test-query", 90, "Descripción menciona sheet music", stamp),
            )
            conn.execute(
                """INSERT INTO archive_files
                   (identifier,file_name,file_name_safe,source_url,archive_format,relevant,
                    inventory_seen_at,download_status,local_path)
                   VALUES(?,?,?,?,?,1,?,'downloaded',?)""",
                ("anthology-test", "score.pdf", "score.pdf",
                 "https://archive.org/download/anthology-test/score.pdf", "PDF", stamp, str(pdf)),
            )
            conn.execute(
                """INSERT INTO archive_content_suggestions
                   (identifier,source_file,line_number,suggestion_text,status,created_at)
                   VALUES(?,?,?,?,'pending',?)""",
                ("anthology-test", "score_djvu.txt", 1, "MOTET IN G", stamp),
            )
        response = client.get("/review")
    assert response.status_code == 200
    assert "Casos que necesitan una mirada" in response.text
    assert "Descripción menciona sheet music" in response.text
    assert "MOTET IN G" in response.text
    assert "/files/view/" in response.text


def test_local_preview_opens_pdf_inline_and_serves_mxl_as_download(tmp_path, monkeypatch):
    db_path = str(tmp_path / "local-preview.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    download_dir = tmp_path / "downloads"
    monkeypatch.setattr(file_catalog, "DOWNLOAD_DIR", download_dir)
    local_dir = download_dir / "anthology-test"
    local_dir.mkdir(parents=True)
    pdf = local_dir / "score.pdf"
    pdf.write_bytes(b"%PDF-1.4 preview")
    mxl = local_dir / "score.mxl"
    mxl.write_bytes(b"PK fake mxl")

    with TestClient(main.app) as client:
        seed_item()
        with main.db() as conn:
            stamp = main.now()
            pdf_id = conn.execute(
                """INSERT INTO archive_files
                   (identifier,file_name,file_name_safe,source_url,inventory_seen_at,
                    download_status,local_path) VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "score.pdf", "score.pdf",
                 "https://archive.org/download/anthology-test/score.pdf", stamp, str(pdf)),
            ).lastrowid
            mxl_id = conn.execute(
                """INSERT INTO archive_files
                   (identifier,file_name,file_name_safe,source_url,inventory_seen_at,
                    download_status,local_path) VALUES(?,?,?,?,?,'downloaded',?)""",
                ("anthology-test", "score.mxl", "score.mxl",
                 "https://archive.org/download/anthology-test/score.mxl", stamp, str(mxl)),
            ).lastrowid
        pdf_response = client.get(f"/files/view/{pdf_id}")
        mxl_response = client.get(f"/files/view/{mxl_id}")

    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"].startswith("application/pdf")
    assert pdf_response.headers["content-disposition"].startswith("inline;")
    assert mxl_response.status_code == 200
    assert mxl_response.headers["content-disposition"].startswith("attachment;")
