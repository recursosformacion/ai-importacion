import app.file_catalog as files


def test_safe_filename_cannot_escape_directory():
    assert files.safe_filename("../../score.pdf") == "score.pdf"
    assert "/" not in files.safe_filename("folder\\\\score.pdf")
    assert files.safe_filename("   ") == "archive-file"


def test_file_classification_is_conservative_and_music_focused():
    assert files.relevant_file("bach.mxl", "")
    assert files.relevant_file("score.pdf", "PDF")
    assert files.relevant_file("pages.jpg", "JPEG")
    assert files.relevant_file("score.capx", "Capella")
    assert files.relevant_file("bach-score.xml", "")
    assert files.relevant_file("bach.xml", "MusicXML")
    assert not files.relevant_file("bach_meta.xml", "Metadata")
    assert not files.relevant_file("bach_files.xml", "XML")
    assert not files.relevant_file("galatheopracmass_jp2.zip", "Single Page Processed JP2 ZIP")
    assert not files.relevant_file("__ia_thumb.jpg", "JPEG Thumb")
    assert not files.relevant_file("metadata.sqlite", "Unknown")


def test_intake_prioritizes_score_xml_over_metadata_xml():
    assert files._intake_priority("item_meta.xml", "Metadata", 50_000) < files._intake_priority(
        "score.pdf", "PDF", 50_000
    )
    assert files._intake_priority("score.xml", "MusicXML", 50_000) > files._intake_priority(
        "score.pdf", "PDF", 50_000
    )
    assert files._intake_priority(
        "galatheopracmass_jp2.zip", "Single Page Processed JP2 ZIP", 98_000_000
    ) < 0
