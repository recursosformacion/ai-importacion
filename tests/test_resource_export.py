from app.resource_export import build_github_resource_document


def test_filename_is_preserved_as_candidate_not_promoted_to_title():
    document = build_github_resource_document(
        {"full_name": "example/scores", "html_url": "https://github.com/example/scores"},
        {
            "path": "Beethoven_Sonata_No_14_Moonlight_Piano.mxl",
            "name": "Beethoven_Sonata_No_14_Moonlight_Piano.mxl",
            "extension": ".mxl",
        },
        {"status": "ok", "metadata": {"title": None, "creators": []}},
        package_file="files/score.mxl",
    )

    assert document["resource"]["title"] is None
    assert document["resource"]["title_candidates"] == [{
        "value": "Beethoven_Sonata_No_14_Moonlight_Piano",
        "source": "source_filename",
    }]


def test_ai_title_candidates_are_preserved_with_their_evidence():
    document = build_github_resource_document(
        {"full_name": "example/scores"},
        {"path": "score.mxl", "name": "score.mxl", "extension": ".mxl"},
        {"status": "ok", "metadata": {
            "title": None,
            "creators": [],
            "title_candidates": [{
                "value": "Sonata No. 14",
                "source": "ai:filename_and_metadata",
                "confidence": 0.61,
            }],
        }},
        package_file="files/score.mxl",
    )

    assert document["resource"]["title"] is None
    assert document["resource"]["title_candidates"][0] == {
        "value": "Sonata No. 14",
        "source": "ai:filename_and_metadata",
        "confidence": 0.61,
    }
    assert document["resource"]["title_candidates"][1]["source"] == "source_filename"


def test_license_policy_fields_travel_with_the_resource():
    document = build_github_resource_document(
        {
            "full_name": "example/scores",
            "license_spdx": "cc0-1.0",
            "license_name": "CC0 1.0",
            "license_status": "hostable",
            "license_checked_at": "2026-11-11T09:00:00+00:00",
            "license_previous_spdx": "cc-by-4.0",
            "license_changed_at": "2026-11-10T09:00:00+00:00",
        },
        {"path": "score.mxl", "name": "score.mxl", "extension": ".mxl"},
        package_file="files/score.mxl",
    )

    license_data = document["source"]["license"]
    assert document["schema"] == "osap-resource-import/1.1"
    assert license_data["status"] == "hostable"
    assert license_data["allow_hosting"] is True
    assert license_data["policy_version"] == "2026-11-11"
    assert license_data["checked_at"] == "2026-11-11T09:00:00+00:00"
    assert license_data["previous_spdx"] == "cc-by-4.0"
    assert license_data["changed_at"] == "2026-11-10T09:00:00+00:00"
    assert license_data["url"] == "https://spdx.org/licenses/CC0-1.0.html"


def test_undeclared_license_is_not_hostable():
    document = build_github_resource_document(
        {"full_name": "example/scores"},
        {"path": "score.mxl", "name": "score.mxl", "extension": ".mxl"},
        package_file="files/score.mxl",
    )

    license_data = document["source"]["license"]
    assert license_data["spdx"] is None
    assert license_data["status"] == "unknown"
    assert license_data["allow_hosting"] is False
    assert license_data["url"] is None
