import json
from fastapi.testclient import TestClient
import app.main as main
import app.file_catalog as file_catalog
import app.workflow as workflow


def test_query_builder():
    assert "mediatype:texts" not in main.query_for("Bach")
    assert main.query_for("Bach", "texts").startswith("mediatype:texts AND ")
    assert "title:(Bach)" in main.query_for("Bach")
    try:
        main.query_for(" ")
    except ValueError:
        pass
    else:
        raise AssertionError("Debe rechazar consulta vacía")


def test_archive_results_are_saved_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.sqlite3"))
    main.init_db()
    with main.db() as conn:
        run_id = conn.execute(
            "INSERT INTO search_runs(query_text,archive_query,rows_requested,started_at) VALUES(?,?,?,?)",
            ("Bach", 'title:"Bach"', 10, main.now()),
        ).lastrowid
    docs = [{"identifier":"bach-test","title":"Bach score","creator":["J. S. Bach"]}]
    assert main.save_results(run_id, docs) == 1
    docs[0]["title"] = "Updated Bach score"
    assert main.save_results(run_id, docs) == 1
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM archive_items WHERE identifier='bach-test'").fetchone()
        assert row["title"] == "Updated Bach score"
        assert json.loads(row["metadata_json"])["identifier"] == "bach-test"


def test_health_and_home(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "web.sqlite3"))
    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert "Captación de partituras" in client.get("/").text


def test_capture_status_remains_readable_during_write_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "capture-status.sqlite3"))
    with TestClient(main.app) as client:
        with main.db() as conn:
            job_id = conn.execute(
                "INSERT INTO capture_jobs(status,started_at,updated_at,message) VALUES('running',?,?,?)",
                (main.now(), main.now(), "Captura en curso"),
            ).lastrowid

        writer = main.db()
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute(
                "UPDATE capture_jobs SET message=? WHERE id=?",
                ("Actualización en curso", job_id),
            )
            response = client.get(f"/capture/status/{job_id}")
        finally:
            writer.rollback()
            writer.close()
    assert response.status_code == 200
    assert response.status_code == 200
    assert "Recopilación automática en curso" in response.text


def test_local_catalog_paginates_and_preserves_filter(tmp_path, monkeypatch):
    db_path = str(tmp_path / "catalog-pagination.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    with TestClient(main.app) as client:
        stamp = main.now()
        with main.db() as conn:
            for index in range(55):
                identifier = f"catalog-item-{index:03d}"
                conn.execute(
                    """INSERT INTO archive_items
                       (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                       VALUES(?,?,?,?,?,?)""",
                    (identifier, f"Catalog score {index}", f"https://archive.org/details/{identifier}",
                     json.dumps({"identifier": identifier}), stamp, stamp),
                )

        first_page = client.get("/items", params={"q": "Catalog", "page_number": 1})
        second_page = client.get("/items", params={"q": "Catalog", "page_number": 2})

    assert first_page.status_code == 200
    assert first_page.text.count("href='/items/catalog-item-") == 50
    assert "Mostrando 1–50 de 55 elementos. Página 1 de 2." in first_page.text
    assert "page_number=2" in first_page.text
    assert second_page.text.count("href='/items/catalog-item-") == 5
    assert "Mostrando 51–55 de 55 elementos. Página 2 de 2." in second_page.text
    assert "q=Catalog" in second_page.text


def test_review_queue_paginates_all_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "review-pagination.sqlite3"))
    with TestClient(main.app) as client:
        stamp = main.now()
        with main.db() as conn:
            for index in range(51):
                identifier = f"review-item-{index:03d}"
                conn.execute(
                    """INSERT INTO archive_items
                       (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                       VALUES(?,?,?,?,?,?)""",
                    (identifier, f"Review score {index}", f"https://archive.org/details/{identifier}",
                     "{}", stamp, stamp),
                )
                conn.execute(
                    """INSERT INTO global_discovery_seen
                       (identifier,first_query_key,score,reason,seen_at) VALUES(?,?,?,?,?)""",
                    (identifier, "test", 10, "score candidate", stamp),
                )
                conn.execute(
                    """INSERT INTO archive_files
                       (identifier,file_name,file_name_safe,source_url,relevant,inventory_seen_at,download_status)
                       VALUES(?,?,?,?,1,?,'downloaded')""",
                    (identifier, "score.sib", "score.sib",
                     f"https://archive.org/download/{identifier}/score.sib", stamp),
                )

        response = client.get("/review?page_number=2")

    assert response.status_code == 200
    assert "Casos 51–51 de 51" in response.text
    assert "página 2 de 2" in response.text
    assert "Anteriores" in response.text
    assert "Siguientes" not in response.text


def test_startup_reclassifies_known_generic_xml_analysis_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "xml-migration.sqlite3"))
    main.init_db()
    stamp = main.now()
    with main.db() as conn:
        for identifier, file_name, archive_format in (
            ("metadata-xml", "item_meta.xml", "Metadata"),
            ("score-xml", "score.musicxml", "MusicXML"),
        ):
            conn.execute(
                """INSERT INTO archive_items
                   (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?)""",
                (identifier, identifier, f"https://archive.org/details/{identifier}", "{}", stamp, stamp),
            )
            file_id = conn.execute(
                """INSERT INTO archive_files
                   (identifier,file_name,file_name_safe,source_url,archive_format,relevant,inventory_seen_at)
                   VALUES(?,?,?,?,?,1,?)""",
                (identifier, file_name, file_name, f"https://archive.org/download/{identifier}/{file_name}",
                 archive_format, stamp),
            ).lastrowid
            conn.execute(
                """INSERT INTO archive_file_analyses
                   (file_id,identifier,analysis_type,status,analysis_json,error_text,analyzed_at)
                   VALUES(?,?,'musicxml','error','{}',?,?)""",
                (file_id, identifier, "El XML no tiene la estructura de una partitura MusicXML.", stamp),
            )

    main.init_db()

    with main.db() as conn:
        metadata = conn.execute(
            """SELECT fa.status,fa.error_text,f.relevant
               FROM archive_file_analyses fa JOIN archive_files f ON f.id=fa.file_id
               WHERE fa.identifier='metadata-xml'"""
        ).fetchone()
        score = conn.execute(
            "SELECT status FROM archive_file_analyses WHERE identifier='score-xml'"
        ).fetchone()
    assert metadata["status"] == "not_musicxml"
    assert metadata["error_text"] is None
    assert metadata["relevant"] == 0
    assert score["status"] == "error"


def test_workflow_escapes_catalog_values_and_page_title(tmp_path, monkeypatch):
    db_path = str(tmp_path / "workflow-escaping.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(workflow, "DB_PATH", db_path)
    with TestClient(main.app) as client:
        stamp = main.now()
        with main.db() as conn:
            identifier = "workflow-escaping-test"
            conn.execute(
                """INSERT INTO archive_items
                   (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?)""",
                (identifier, "<script>alert(1)</script>", "https://archive.org/details/" + identifier,
                 json.dumps({"identifier": identifier}), stamp, stamp),
            )
            conn.execute(
                """INSERT INTO global_discovery_seen
                   (identifier,first_query_key,score,reason,seen_at) VALUES(?,?,?,?,?)""",
                (identifier, "test", 1, "test", stamp),
            )
            conn.execute(
                """INSERT INTO archive_files
                   (identifier,file_name,file_name_safe,source_url,archive_format,relevant,
                    inventory_seen_at,download_status,local_path)
                   VALUES(?,?,?,?,?,1,?,'downloaded',?)""",
                (identifier, "<img src=x onerror=alert(1)>.pdf", "image.pdf",
                 "https://archive.org/download/" + identifier + "/image.pdf", "PDF", stamp,
                 "C:\\downloads\\<script>alert(1)</script>.pdf"),
            )
        response = workflow.workflow_home()

    rendered = response.body.decode()
    assert "<script>alert(1)</script>" not in rendered
    assert "<img src=x onerror=alert(1)>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert workflow.layout("<svg onload=alert(1)>", "").body.decode().count("<svg onload=alert(1)>") == 0


def test_rights_review_stores_evidence_history_and_escapes_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "rights-review.sqlite3"))
    stamp = main.now()
    with TestClient(main.app) as client:
        with main.db() as conn:
            conn.execute(
                """INSERT INTO archive_items
                   (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?)""",
                ("rights-review-test", "Rights test", "https://archive.org/details/rights-review-test",
                 json.dumps({"identifier": "rights-review-test"}), stamp, stamp),
            )
        response = client.post(
            "/items/rights-review-test/rights-status",
            data={
                "status": "evidence_found",
                "evidence_url": "https://example.org/license",
                "basis": "License page",
                "jurisdiction": "ES",
                "notes": "<script>alert(1)</script>",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        detail = client.get("/items/rights-review-test")
        invalid_url = client.post(
            "/items/rights-review-test/rights-status",
            data={"status": "public_domain_confirmed", "evidence_url": "javascript:alert(1)"},
        )

    assert "href='https://example.org/license'" in detail.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail.text
    assert "<script>alert(1)</script>" not in detail.text
    assert invalid_url.status_code == 422
    with main.db() as conn:
        item = conn.execute(
            "SELECT rights_status FROM archive_items WHERE identifier='rights-review-test'"
        ).fetchone()
        reviews = conn.execute(
            "SELECT status,evidence_url,basis,jurisdiction,notes FROM archive_rights_reviews WHERE identifier=?",
            ("rights-review-test",),
        ).fetchall()
    assert item["rights_status"] == "evidence_found"
    assert len(reviews) == 1
    assert tuple(reviews[0]) == (
        "evidence_found", "https://example.org/license", "License page", "ES",
        "<script>alert(1)</script>",
    )



def test_review_shows_unsupported_downloaded_format_with_real_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "review.sqlite3"))
    with TestClient(main.app) as client:
        with main.db() as conn:
            stamp = main.now()
            conn.execute(
                """INSERT INTO archive_items
                   (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?)""",
                ("score-sibelius", "Partitura Sibelius", "https://archive.org/details/score-sibelius",
                 json.dumps({"identifier": "score-sibelius"}), stamp, stamp),
            )
            conn.execute(
                """INSERT INTO global_discovery_seen
                   (identifier,first_query_key,score,reason,seen_at)
                   VALUES(?,?,?,?,?)""",
                ("score-sibelius", "test-query", 80, "Metadatos de partitura", stamp),
            )
            conn.execute(
                """INSERT INTO archive_files
                   (identifier,file_name,file_name_safe,source_url,archive_format,relevant,
                    inventory_seen_at,download_status,local_path)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("score-sibelius", "score.sib", "score.sib",
                 "https://archive.org/download/score-sibelius/score.sib", "Sibelius", 1,
                 stamp, "downloaded", str(tmp_path / "score.sib")),
            )
        response = client.get("/review")
        assert response.status_code == 200
        assert "Partitura Sibelius" in response.text
        assert "formato todavía no puede interpretar automáticamente" in response.text
        assert "Disponible para descargar" not in response.text


def test_music_candidate_filter_rejects_irrelevant_and_prioritizes_scores():
    score, reason = main.music_candidate_score({
        "title": "Choral sheet music for mixed voices",
        "subject": ["Sheet music", "Choral music"],
    })
    assert score > 0
    assert "Indicios" in reason

    score, reason = main.music_candidate_score({
        "title": "Revision of glioblastoma proliferation research",
        "description": "Scientific systematic review",
    })
    assert score == 0
    assert "Descartado" in reason

    # A musical genre in an audio item is not enough to make it a score candidate.
    score, reason = main.music_candidate_score({
        "identifier": "symphony-recording",
        "title": "Symphony No. 5 - complete recording",
        "mediatype": "audio",
        "format": ["MP3", "VBR MP3"],
    })
    assert score == 0
    assert "audiovisual" in reason

    # Explicit score evidence in the title remains eligible even if the source
    # item has an audiovisual media type; the content still needs inspection.
    score, reason = main.music_candidate_score({
        "title": "Ave Maria sheet music",
        "mediatype": "texts",
        "format": ["PDF"],
    })
    assert score > 0


def test_global_discovery_uses_persistent_cursors_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "discover.sqlite3"))
    calls = []

    async def fake_archive_scrape(query, count, cursor=None, query_key='default'):
        assert count == 100
        calls.append((query, cursor))
        return {
            "total": 900,
            "items": [
                {"identifier": "score-good", "title": "Sheet music: Ave Maria", "subject": ["Vocal score"]},
                {"identifier": "paper-bad", "title": "Scientific review", "description": "glioblastoma systematic review"},
                {"identifier": "unknown", "title": "Untitled item"},
            ],
            "cursor": "next-cursor" if cursor is None else None,
        }

    monkeypatch.setattr(main, "archive_scrape", fake_archive_scrape)
    with TestClient(main.app) as client:
        first = client.post("/discover", data={"rows": 100})
        assert first.status_code == 200
        assert "18</b> registros" in first.text
        assert "1 candidatos de este lote superaron el filtro" in first.text
        with main.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_seen").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_queries WHERE status='active'").fetchone()[0] == 6
        second = client.post("/discover", data={"rows": 100})
        assert second.status_code == 200
        assert "llegado al final de sus cursores" in second.text
        assert len(calls) == 12
        assert all(cursor is None for _, cursor in calls[:6])
        assert all(cursor == "next-cursor" for _, cursor in calls[6:])
        with main.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_seen").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_queries WHERE status='completed'").fetchone()[0] == 6

def test_batch_inventory_records_unknown_and_uncommon_formats_and_is_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "inventory.sqlite3"))
    main.init_db()
    with main.db() as conn:
        stamp = main.now()
        conn.execute("""INSERT INTO archive_items
            (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?)""",
            ("score-inventory-test", "Sibelius score", "https://archive.org/details/score-inventory-test",
             json.dumps({"identifier":"score-inventory-test","title":"Sibelius sheet music"}), stamp, stamp))
        conn.execute("""INSERT INTO global_discovery_seen(identifier,first_query_key,score,reason,seen_at)
            VALUES(?,?,?,?,?)""", ("score-inventory-test", "scores_titles", 5, "sheet music", stamp))

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"files": [
                {"name": "piece.sib", "format": "Sibelius", "size": "123", "md5": "abc"},
                {"name": "mystery.zzz", "format": "Unknown Binary", "size": "456"},
            ]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            assert "score-inventory-test" in url
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    with TestClient(main.app) as client:
        response = client.post("/inventory-candidates", data={"batch_size": 10})
        assert response.status_code == 200
        assert "2" in response.text
        with main.db() as conn:
            rows = conn.execute("SELECT file_name,relevant,archive_format FROM archive_files ORDER BY file_name").fetchall()
            assert [(r["file_name"], r["archive_format"]) for r in rows] == [
                ("mystery.zzz", "Unknown Binary"), ("piece.sib", "Sibelius")
            ]
            assert conn.execute("SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok' AND file_count=2").fetchone()[0] == 1
        # A successful zero-or-more-file inventory is not repeated on the next batch.
        again = client.post("/inventory-candidates", data={"batch_size": 10})
        assert "0</b> elementos" in again.text


def test_batch_inventory_records_zero_files_and_retries_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "inventory-retry.sqlite3"))
    main.init_db()
    with main.db() as conn:
        stamp = main.now()
        for ident in ("empty-inventory-test", "retry-inventory-test"):
            conn.execute("""INSERT INTO archive_items
                (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?)""",
                (ident, ident, "https://archive.org/details/" + ident,
                 json.dumps({"identifier":ident,"title":"sheet music"}), stamp, stamp))
            conn.execute("""INSERT INTO global_discovery_seen(identifier,first_query_key,score,reason,seen_at)
                VALUES(?,?,?,?,?)""", (ident, "scores_titles", 3, "sheet music", stamp))

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    calls = {"retry-inventory-test": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            ident = "retry-inventory-test" if "retry-inventory-test" in url else "empty-inventory-test"
            if ident == "retry-inventory-test":
                calls[ident] += 1
                if calls[ident] == 1:
                    raise RuntimeError("temporary failure")
                return FakeResponse({"files": []})
            return FakeResponse({"files": []})

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    with TestClient(main.app) as client:
        first = client.post("/inventory-candidates", data={"batch_size": 10})
        assert first.status_code == 200
        with main.db() as conn:
            row = conn.execute("SELECT status,file_count FROM archive_item_inventories WHERE identifier='empty-inventory-test'").fetchone()
            assert (row["status"], row["file_count"]) == ("ok", 0)
            assert conn.execute("SELECT status FROM archive_item_inventories WHERE identifier='retry-inventory-test'").fetchone()["status"] == "error"
        second = client.post("/inventory-candidates", data={"batch_size": 10})
        assert second.status_code == 200
        with main.db() as conn:
            assert conn.execute("SELECT status FROM archive_item_inventories WHERE identifier='retry-inventory-test'").fetchone()["status"] == "ok"
        assert calls["retry-inventory-test"] == 2


def test_download_requires_successful_inventory(tmp_path, monkeypatch):
    db_path = str(tmp_path / "download-guard.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(file_catalog, "DB_PATH", db_path)
    main.init_db()
    with main.db() as conn:
        stamp = main.now()
        conn.execute("""INSERT INTO archive_items
            (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?)""",
            ("guard-test", "Guard test", "https://archive.org/details/guard-test",
             json.dumps({"identifier": "guard-test"}), stamp, stamp))
        conn.execute("""INSERT INTO archive_files
            (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,relevant,inventory_seen_at)
            VALUES(?,?,?,?,?,?,1,?)""",
            ("guard-test", "score.pdf", "score.pdf",
             "https://archive.org/download/guard-test/score.pdf", 10, "PDF", stamp))
        file_id = conn.execute("SELECT id FROM archive_files WHERE identifier='guard-test'").fetchone()[0]

    with TestClient(main.app) as client:
        response = client.post(f"/files/download/{file_id}", follow_redirects=False)
        assert response.status_code == 409
        assert "inventario correcto registrado" in response.json()["detail"]
        with main.db() as conn:
            assert conn.execute(
                "SELECT download_status FROM archive_files WHERE id=?", (file_id,)
            ).fetchone()["download_status"] == "not_downloaded"
