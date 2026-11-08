from app.github_acquisition import extract_reference_candidates, resolve_manifest_entries
from app.github_catalog import parse_repository
from app.resource_export import build_github_resource_document, encode_resource_json, stable_resource_id


def test_extracts_public_attribution_and_named_project_link():
    readme = (
        "For public use:\n\n"
        "We kindly ask that, in any public use of these scores, you mention "
        "[OpenScore String Quartets](https://musescore.com/openscore-string-quartets)."
    )

    references = extract_reference_candidates(readme, "README.md")

    assert len(references) == 1
    reference = references[0]
    assert reference["reference_type"] == "attribution"
    assert reference["label"] == "OpenScore String Quartets"
    assert reference["display_text"] == "OpenScore String Quartets"
    assert reference["url"] == "https://musescore.com/openscore-string-quartets"
    assert "public use" in reference["instruction_text"].lower()
    assert reference["source_path"] == "README.md"
    assert reference["required"] == 1


def test_extracts_spanish_attribution_entity_and_url():
    readme = (
        "Les pedimos amablemente que, en cualquier uso público de estas partituras, "
        "mencionen a OpenScore String Quartets y proporcionen un enlace a "
        "[OSSQ o a este repositorio.](https://musescore.com/openscore-string-quartets)"
    )

    references = extract_reference_candidates(readme, "README.md")

    assert len(references) == 1
    assert references[0]["label"] == "OpenScore String Quartets"
    assert references[0]["display_text"] == "OpenScore String Quartets"
    assert references[0]["url"] == "https://musescore.com/openscore-string-quartets"
    assert "uso público" in references[0]["instruction_text"].lower()
    assert references[0]["required"] == 1


def test_does_not_treat_unrelated_readme_links_as_attribution():
    readme = (
        "## Installation\n\n"
        "See [the documentation](https://example.org/docs) for setup."
    )

    assert extract_reference_candidates(readme, "README.md") == []


def test_extracts_plain_url_and_project_name_from_credit_request():
    readme = (
        "We kindly ask that you credit OpenScore Lieder and provide a link to "
        "https://musescore.com/openscore-lieder-corpus for any public-facing use."
    )

    references = extract_reference_candidates(readme, "README.md")

    assert len(references) == 1
    assert references[0]["label"] == "OpenScore Lieder"
    assert references[0]["display_text"] == "OpenScore Lieder"
    assert references[0]["url"] == "https://musescore.com/openscore-lieder-corpus"



def test_manifest_paths_are_resolved_relative_to_manifest_and_checked_against_tree():
    manifest = [
        {"in": "../scores/Composer/Set/Song/lc123.mscx",
         "out": "../scores/Composer/Set/Song/lc123.mxl"},
        {"in": "../../outside.mscx", "out": "../outside.mxl"},
    ]

    result = resolve_manifest_entries(
        "data/corpus_conversion.json",
        manifest,
        {"scores/Composer/Set/Song/lc123.mscx"},
    )

    assert result["entry_count"] == 2
    assert result["input_paths"] == ["scores/Composer/Set/Song/lc123.mscx"]
    assert result["output_paths"] == ["scores/Composer/Set/Song/lc123.mxl"]
    assert result["missing_input_paths"] == []
    assert result["invalid_path_count"] == 2


def test_musetrainer_library_page_resolves_to_github_repository():
    assert parse_repository("https://musetrainer.github.io/library/") == ("musetrainer", "library")


def test_resource_json_preserves_available_musicxml_metadata_and_one_file():
    repository = {
        "id": 7,
        "full_name": "musetrainer/library",
        "html_url": "https://github.com/musetrainer/library",
        "description": "MusicXML library",
        "default_branch": "main",
        "license_spdx": "CC-BY-4.0",
        "license_name": "Creative Commons Attribution 4.0",
    }
    source_file = {
        "id": 123,
        "path": "scores/mozart/ave_verum.mxl",
        "name": "ave_verum.mxl",
        "extension": ".mxl",
        "size_bytes": 1200,
        "downloaded_bytes": 1180,
        "sha256": "abc123",
        "git_sha": "def456",
        "html_url": "https://github.com/musetrainer/library/blob/main/scores/mozart/ave_verum.mxl",
        "download_url": "https://raw.githubusercontent.com/musetrainer/library/main/scores/mozart/ave_verum.mxl",
        "first_seen_at": "2026-11-01T10:00:00+00:00",
        "last_seen_at": "2026-11-02T10:00:00+00:00",
    }
    analysis = {
        "type": "musicxml",
        "status": "ok",
        "metadata": {
            "format": "mxl",
            "status": "ok",
            "title": "Ave Verum Corpus",
            "creators": [{"name": "W. A. Mozart", "type": "composer"}],
            "parts": [{"id": "P1", "name": "Soprano", "measures": 46}],
        },
    }
    document = build_github_resource_document(
        repository, source_file, analysis, package_file="files/resource.mxl"
    )

    assert document["resource"]["title"] == "Ave Verum Corpus"
    assert document["resource"]["creators"][0]["type"] == "composer"
    assert document["file"]["package_path"] == "files/resource.mxl"
    assert document["source"]["file_path"] == "scores/mozart/ave_verum.mxl"
    assert document["source"]["license"]["verified_for_file"] is False
    assert document["resource_key"] == stable_resource_id(
        "musetrainer/library", "scores/mozart/ave_verum.mxl"
    )
    assert b'"Ave Verum Corpus"' in encode_resource_json(document)


def test_resource_json_keeps_musicxml_title_evidence_and_filename_candidate():
    document = build_github_resource_document(
        {"full_name": "example/scores"},
        {
            "path": "scores/mozart/Ave_Verum_Corpus.mxl",
            "name": "Ave_Verum_Corpus.mxl",
            "extension": ".mxl",
        },
        {
            "status": "ok",
            "metadata": {
                "title": "Ave Verum Corpus",
                "work_title": "Ave Verum Corpus",
                "movement_title": "Adagio",
                "title_source": "work-title",
                "creators": [{"name": "W. A. Mozart", "type": "composer"}],
                "instrumentation": ["Soprano", "Alto", "Tenor", "Bass"],
            },
        },
        package_file="files/example.mxl",
    )

    assert document["resource"]["title"] == "Ave Verum Corpus"
    assert document["resource"]["title_source"] == "work-title"
    assert document["resource"]["title_candidates"] == [
        {"value": "Ave_Verum_Corpus", "source": "source_filename"}
    ]
    assert document["resource"]["instrumentation"] == [
        "Soprano", "Alto", "Tenor", "Bass"
    ]


def test_repository_download_worker_uses_bounded_parallelism(tmp_path, monkeypatch):
    import asyncio
    import app.github_catalog as catalog

    monkeypatch.setattr(catalog, "DB_PATH", str(tmp_path / "parallel-download.sqlite3"))
    monkeypatch.setattr(catalog, "DOWNLOAD_CONCURRENCY", 4)
    catalog.ensure_schema()
    with catalog.db() as conn:
        conn.execute(
            """INSERT INTO github_repositories(full_name,html_url,default_branch,added_at)
               VALUES('example/scores','https://github.com/example/scores','main','2026-01-01')"""
        )
        conn.execute(
            """INSERT INTO github_download_jobs(repository_id,status,total,started_at,updated_at)
               VALUES(1,'queued',8,'','')"""
        )
        for index in range(8):
            conn.execute(
                """INSERT INTO github_files(repository_id,path,name,extension,first_seen_at,last_seen_at)
                   VALUES(1,?,?,'.mxl','2026-01-01','2026-01-01')""",
                (f"score-{index}.mxl", f"score-{index}.mxl"),
            )

    active = 0
    peak = 0

    async def fake_download(file_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        with catalog.db() as conn:
            conn.execute(
                "UPDATE github_files SET download_status='downloaded' WHERE id=?",
                (file_id,),
            )
        active -= 1

    monkeypatch.setattr(catalog, "github_download_file", fake_download)
    asyncio.run(catalog._run_repository_downloads(1, list(range(1, 9))))

    with catalog.db() as conn:
        job = conn.execute(
            "SELECT status,total,completed,failed FROM github_download_jobs WHERE repository_id=1"
        ).fetchone()
    assert peak == 4
    assert tuple(job) == ("completed", 8, 8, 0)


def test_download_status_reports_cumulative_repository_progress(tmp_path, monkeypatch):
    import json
    import app.github_catalog as catalog

    monkeypatch.setattr(catalog, "DB_PATH", str(tmp_path / "cumulative-progress.sqlite3"))
    catalog.ensure_schema()
    with catalog.db() as conn:
        conn.execute(
            """INSERT INTO github_repositories(full_name,html_url,default_branch,added_at)
               VALUES('example/scores','https://github.com/example/scores','main','2026-01-01')"""
        )
        conn.execute(
            """INSERT INTO github_download_jobs(repository_id,status,total,completed,failed,started_at,updated_at)
               VALUES(1,'interrupted',1000,60,0,'','')"""
        )
        for index in range(660):
            status = "downloaded" if index < 660 else "not_downloaded"
            conn.execute(
                """INSERT INTO github_files
                   (repository_id,path,name,extension,download_status,first_seen_at,last_seen_at)
                   VALUES(1,?,?,'.mxl',?,'2026-01-01','2026-01-01')""",
                (f"score-{index}.mxl", f"score-{index}.mxl", status),
            )
        for index in range(10):
            conn.execute(
                """INSERT INTO github_files
                   (repository_id,path,name,extension,download_status,first_seen_at,last_seen_at)
                   VALUES(1,?,?,'.mxl','not_downloaded','2026-01-01','2026-01-01')""",
                (f"pending-{index}.mxl", f"pending-{index}.mxl"),
            )
        conn.execute(
            """INSERT INTO github_files
               (repository_id,path,name,extension,download_status,first_seen_at,last_seen_at)
               VALUES(1,'large.mxl','large.mxl','.mxl','too_large','2026-01-01','2026-01-01')"""
        )

    response = catalog.github_download_status(1)
    payload = json.loads(response.body)

    assert payload["status"] == "interrupted"
    assert payload["completed"] == 660
    assert payload["total"] == 670
    assert payload["pending"] == 10
    assert payload["too_large"] == 1
    assert payload["job_completed"] == 60
