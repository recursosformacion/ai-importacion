import app.file_catalog as files


def test_safe_filename_cannot_escape_directory():
    assert files.safe_filename("../../score.pdf") == "score.pdf"
    assert "/" not in files.safe_filename("folder\\\\score.pdf")
    assert files.safe_filename("   ") == "archive-file"


def test_file_classification_is_conservative_and_music_focused():
    assert files.relevant_file("bach.mxl", "")
    assert files.relevant_file("score.pdf", "PDF")
    assert files.relevant_file("pages.jpg", "JPEG")
    assert not files.relevant_file("metadata.sqlite", "Unknown")
