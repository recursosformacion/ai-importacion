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
