from pathlib import Path

import app.github_processing as processing


def test_secrets_file_reads_only_gemini_keys_and_rotates(tmp_path, monkeypatch):
    secret_file = tmp_path / "secrets.txt"
    secret_file.write_text(
        "# local only\nGEMINI_API_KEYS=key-one,key-two\nOTHER_SECRET=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_IMPORTACION_SECRETS", str(secret_file))
    monkeypatch.setenv("GEMINI_API_KEY", "key-three")

    assert processing._read_secrets_file() == ["key-three", "key-one", "key-two"]


def test_secrets_file_accepts_a_plain_single_key(tmp_path, monkeypatch):
    secret_file = tmp_path / "secrets.txt"
    secret_file.write_text("plain-local-key\n", encoding="utf-8")
    monkeypatch.setenv("AI_IMPORTACION_SECRETS", str(secret_file))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)

    assert processing._read_secrets_file() == ["plain-local-key"]


def test_gemini_json_parser_accepts_markdown_fence():
    result = processing._json_from_model(
        '```json\n{"title":"Ave verum corpus","composer":"W. A. Mozart"}\n```'
    )

    assert result["title"] == "Ave verum corpus"
    assert result["composer"] == "W. A. Mozart"


def test_musicxml_explicit_title_is_read_from_content(tmp_path):
    score = tmp_path / "unknown-name.musicxml"
    score.write_text(
        """<?xml version="1.0"?>
        <score-partwise version="4.0">
          <work><work-title>Ave verum corpus</work-title></work>
        </score-partwise>""",
        encoding="utf-8",
    )

    assert processing._explicit_title(score, ".musicxml") == "Ave verum corpus"


def test_local_context_extracts_text_without_using_filename(tmp_path):
    score = tmp_path / "not-the-title.abc"
    score.write_text("T:Ave verum corpus\nC:W. A. Mozart\nK:D\n", encoding="utf-8")

    context = processing._extract_context(score)

    assert "T:Ave verum corpus" in context
    assert "not-the-title" not in context


def test_full_snapshot_clone_does_not_use_partial_blob_filter(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, cwd=None, timeout=1800):
        calls.append(args)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(processing, "_run", fake_run)
    target = tmp_path / "repo"
    processing._clone_complete_snapshot("https://github.com/example/music.git", target)

    clone_args = calls[0]
    assert "clone" in clone_args
    assert "--depth=1" in clone_args
    assert "--no-recurse-submodules" in clone_args
    assert "--filter=blob:none" not in clone_args
    assert "--no-checkout" not in clone_args
    assert target.is_dir()
