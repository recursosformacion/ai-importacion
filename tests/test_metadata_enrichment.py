from __future__ import annotations

import json

import app.metadata_enrichment as enrichment


def test_ai_metadata_accepts_only_high_confidence_typed_values():
    result = enrichment.validate_ai_metadata({
        "title": "Sonata No. 14, Moonlight",
        "title_confidence": 0.94,
        "creators": [
            {"name": "Ludwig van Beethoven", "type": "composer", "confidence": 0.98},
            {"name": "Unknown credit", "type": "composer", "confidence": 0.31},
            {"name": "Piano", "type": "instrument", "confidence": 0.99},
        ],
    })

    assert result["title"] == "Sonata No. 14, Moonlight"
    assert result["creators"] == [{
        "name": "Ludwig van Beethoven",
        "type": "composer",
        "source": "ai:filename_and_metadata",
        "confidence": 0.98,
    }]


def test_ai_metadata_does_not_promote_low_confidence_title():
    result = enrichment.validate_ai_metadata({
        "title": "Beethoven Sonata piano",
        "title_confidence": 0.42,
        "creators": [],
    })

    assert result["title"] is None
    assert result["title_candidates"][0]["value"] == "Beethoven Sonata piano"


def test_ai_metadata_returns_not_configured_without_api_key(monkeypatch):
    for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(enrichment, "_configured_keys", lambda: [])
    result = enrichment.enrich_music_metadata(
        filename="Beethoven_Sonata_No_14_Moonlight_Piano.mxl",
        repository={"full_name": "example/scores", "description": "Classical scores"},
        metadata={},
    )
    assert result["status"] == "not_configured"


def test_ai_metadata_uses_gemini_and_parses_json(monkeypatch):
    monkeypatch.setattr(enrichment, "_configured_keys", lambda: ["test-key"])
    response_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "title": "Sonata No. 14",
                        "title_confidence": 0.91,
                        "creators": [{
                            "name": "Ludwig van Beethoven",
                            "type": "composer",
                            "confidence": 0.97,
                        }],
                    })
                }]
            }
        }]
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return response_payload

    def fake_post(url, json, timeout):
        assert "gemini-flash-lite-latest" in url
        assert "Beethoven_Sonata_No_14" in json["contents"][0]["parts"][0]["text"]
        return FakeResponse()

    monkeypatch.setattr(enrichment.httpx, "post", fake_post)
    result = enrichment.enrich_music_metadata(
        filename="Beethoven_Sonata_No_14_Moonlight_Piano.mxl",
        repository={"full_name": "example/scores", "description": "Classical scores"},
        metadata={},
    )

    assert result["status"] == "ok"
    assert result["title"] == "Sonata No. 14"
    assert result["creators"][0]["name"] == "Ludwig van Beethoven"
