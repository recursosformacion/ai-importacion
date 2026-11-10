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


def test_music_snapshot_uses_one_filtered_sparse_checkout_batch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fake_run(args, cwd=None, timeout=1800, input_text=None):
        calls.append((args, input_text))
        if "clone" in args:
            target = Path(args[-1])
            (target / ".git").mkdir(parents=True)
            (target / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            return SimpleNamespace(stdout="", stderr="")
        if args[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout="abc123\n", stderr="")
        if args[:3] == ["git", "ls-tree", "-r"]:
            return SimpleNamespace(
                stdout="score.musicxml\0README.md\0nested/score.mxl\0",
                stderr="",
            )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(processing, "_run", fake_run)
    target = tmp_path / "repo"

    commit, selected = processing._prepare_music_snapshot(
        "https://github.com/example/music.git", target
    )

    assert commit == "abc123"
    assert selected == ["nested/score.mxl", "score.musicxml"]
    clone_args = calls[0][0]
    assert "clone" in clone_args
    assert "--filter=blob:none" in clone_args
    assert "--no-checkout" in clone_args
    assert "--no-recurse-submodules" in clone_args

    sparse_set = next(call for call in calls if call[0][:4] == [
        "git", "sparse-checkout", "set", "--no-cone"
    ])
    assert sparse_set[0][-1] == "--stdin"
    assert sparse_set[1] == "nested/score.mxl\nscore.musicxml\n"
    assert any(call[0][:3] == ["git", "checkout", "--force"] for call in calls)
    assert not any(call[0][:2] == ["git", "show"] for call in calls)
