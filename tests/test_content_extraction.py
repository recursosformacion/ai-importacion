from zipfile import ZipFile

from app.content_extraction import extract_local_content


def test_extracts_plain_text_without_provider_database(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Kyrie eleison\nComposer: Example", encoding="utf-8")

    result = extract_local_content(path)

    assert result["status"] == "text_checked"
    assert "Kyrie eleison" in result["text"]
    assert result["extracted_characters"] > 0


def test_extracts_notation_from_mscz_container(tmp_path):
    path = tmp_path / "score.mscz"
    with ZipFile(path, "w") as archive:
        archive.writestr("score.mscx", "<museScore><Score><workTitle>Test Motet</workTitle></Score></museScore>")

    result = extract_local_content(path)

    assert result["status"] == "text_checked"
    assert "Test Motet" in result["text"]


def test_missing_file_is_reported(tmp_path):
    result = extract_local_content(tmp_path / "missing.pdf")

    assert result["status"] == "error"
    assert "No existe" in result["error_text"]


def test_unsupported_file_is_not_treated_as_extraction_error(tmp_path):
    path = tmp_path / "score.sib"
    path.write_bytes(b"binary")

    result = extract_local_content(path)

    assert result["status"] == "unsupported"
    assert result["text"] == ""
