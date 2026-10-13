import json
from fastapi.testclient import TestClient
import app.main as main


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
