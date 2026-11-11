import sqlite3
from pathlib import Path

import app.main as main
from app.file_catalog import normalize_file_records
from app.musicxml_analysis import analyze_lilypond
from app.rights_policy import canonical_license, distribution_decision, transcriber_credit


def test_only_file_level_cc_by_and_cc0_are_automatically_hosted():
    assert distribution_decision("score.mxl", "CC-BY-4.0", license_scope="file") == ("redistributable", "hosted")
    assert distribution_decision("score.ly", "CC0-1.0", license_scope="file") == ("redistributable", "hosted")
    assert distribution_decision("score.mxl", "CC-BY-NC-4.0", license_scope="file") == ("restricted_or_unclear", "external_link")
    assert distribution_decision("score.mxl", "CC-BY-4.0", license_scope="repository") == ("unverified", "external_link")
    assert distribution_decision("score.mxl", None, license_scope="unknown") == ("unverified", "external_link")


def test_license_url_is_recognized_but_nc_is_not():
    assert canonical_license(None, "https://creativecommons.org/licenses/by/4.0/") == "CC-BY-4.0"
    assert canonical_license(None, "https://creativecommons.org/licenses/by-nc/4.0/") is None


def test_transcriber_credit_is_not_confused_with_composer():
    assert transcriber_credit({"creators": [
        {"name": "Composer", "type": "composer"},
        {"name": "Transcriber", "type": "transcriber"},
    ]}) == "Transcriber"


def test_lilypond_metadata_includes_transcriber_and_license(tmp_path):
    path = tmp_path / "score.ly"
    path.write_text('''% Transcribed by: Ada Transcriber
% License: CC-BY-4.0 https://creativecommons.org/licenses/by/4.0/
\\header {
  title = "Ave verum"
  composer = "W. A. Mozart"
}
''', encoding="utf-8")
    result = analyze_lilypond(path)
    assert result["title"] == "Ave verum"
    assert result["license_spdx"] == "CC-BY-4.0"
    assert transcriber_credit(result) == "Ada Transcriber"


def test_archive_file_inventory_uses_only_file_level_license_metadata():
    rows = normalize_file_records("rights-test", [{
        "name": "score.mxl",
        "format": "Compressed MusicXML",
        "license_spdx": "CC-BY-4.0",
    }], "2026-11-11T00:00:00+00:00")
    assert rows[0]["license_scope"] == "file"
    assert rows[0]["rights_status"] == "redistributable"
    assert rows[0]["distribution_mode"] == "hosted"


def test_startup_migrates_legacy_unverified_mxl_to_external_only(tmp_path, monkeypatch):
    db_path = tmp_path / "migration.sqlite3"
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    score = download_root / "legacy-score.mxl"
    score.write_bytes(b"old score")
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "DOWNLOAD_DIR", download_root)

    main.init_db()
    stamp = main.now()
    with main.db() as conn:
        conn.execute(
            """INSERT INTO archive_items
               (identifier,title,source_url,metadata_json,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?,?)""",
            ("legacy-rights-test", "Legacy", "https://archive.org/details/legacy-rights-test", "{}", stamp, stamp),
        )
        conn.execute(
            """INSERT INTO archive_files
               (identifier,file_name,file_name_safe,source_url,inventory_seen_at,
                download_status,local_path)
               VALUES(?,?,?,?,?,'downloaded',?)""",
            ("legacy-rights-test", "legacy-score.mxl", "legacy-score.mxl",
             "https://archive.org/download/legacy-rights-test/legacy-score.mxl", stamp, str(score)),
        )

    main.init_db()
    with main.db() as conn:
        row = conn.execute(
            """SELECT download_status,local_path,distribution_mode,rights_status
               FROM archive_files WHERE identifier='legacy-rights-test'"""
        ).fetchone()
    assert row["download_status"] == "external_only"
    assert row["local_path"] is None
    assert row["distribution_mode"] == "external_link"
    assert row["rights_status"] == "unverified"
    assert not score.exists()
