import json
from fastapi.testclient import TestClient
import app.main as main
import app.file_catalog as file_catalog


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
        assert "15</b> registros" in first.text
        assert "1 candidatos de este lote superaron el filtro" in first.text
        with main.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_seen").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_queries WHERE status='active'").fetchone()[0] == 5
        second = client.post("/discover", data={"rows": 100})
        assert second.status_code == 200
        assert "llegado al final de sus cursores" in second.text
        assert len(calls) == 10
        assert all(cursor is None for _, cursor in calls[:5])
        assert all(cursor == "next-cursor" for _, cursor in calls[5:])
        with main.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_seen").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM global_discovery_queries WHERE status='completed'").fetchone()[0] == 5

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
