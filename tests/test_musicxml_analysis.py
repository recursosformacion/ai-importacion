from zipfile import ZipFile

import pytest

from app.musicxml_analysis import analyze_musicxml


SCORE = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <work><work-title>Test Motet</work-title></work>
  <identification><creator type="composer">Ada Composer</creator></identification>
  <part-list><score-part id="P1"><part-name>Soprano</part-name></score-part>
    <score-part id="P2"><part-name>Organ</part-name></score-part></part-list>
  <part id="P1"><measure number="1"/><measure number="2"/></part>
  <part id="P2"><measure number="1"/><measure number="2"/><measure number="3"/></part>
</score-partwise>"""


def test_analyze_musicxml_extracts_title_creator_parts_and_measures(tmp_path):
    path = tmp_path / "score.musicxml"
    path.write_text(SCORE, encoding="utf-8")
    result = analyze_musicxml(path)
    assert result["status"] == "ok"
    assert result["title"] == "Test Motet"
    assert result["creators"] == [{"name": "Ada Composer", "type": "composer"}]
    assert result["part_count"] == 2
    assert result["measure_count"] == 3
    assert [part["name"] for part in result["parts"]] == ["Soprano", "Organ"]


def test_analyze_mxl_reads_musicxml_from_container(tmp_path):
    path = tmp_path / "score.mxl"
    with ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", """<container><rootfiles>
          <rootfile full-path="scores/main.musicxml" media-type="application/vnd.recordare.musicxml+xml"/>
        </rootfiles></container>""")
        archive.writestr("scores/main.musicxml", SCORE)
    result = analyze_musicxml(path)
    assert result["format"] == "mxl"
    assert result["title"] == "Test Motet"


def test_analyze_musicxml_rejects_non_score_xml(tmp_path):
    path = tmp_path / "not-score.xml"
    path.write_text("<metadata><title>Not a score</title></metadata>", encoding="utf-8")
    with pytest.raises(ValueError, match="no tiene la estructura"):
        analyze_musicxml(path)
