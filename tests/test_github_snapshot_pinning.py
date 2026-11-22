from types import SimpleNamespace

import pytest

from app import github_processing


def test_prepare_music_snapshot_rejects_commit_outside_legal_snapshot(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, cwd=None, timeout=1800, input_text=None):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return SimpleNamespace(stdout="newer-unreviewed-commit\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(github_processing, "_run", fake_run)

    with pytest.raises(ValueError, match="cambió después de la instantánea legal"):
        github_processing._prepare_music_snapshot(
            "https://github.com/example/music.git",
            tmp_path / "repo",
            expected_commit="approved-commit",
        )

    assert len(calls) == 2
    assert calls[0][0:2] == ["git", "-c"]
    assert calls[1] == ["git", "rev-parse", "FETCH_HEAD"]
