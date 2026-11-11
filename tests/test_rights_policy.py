from app.musicxml_analysis import analyze_lilypond
from app.rights_policy import canonical_license, distribution_decision, transcriber_credit


def test_only_file_level_cc_by_and_cc0_are_automatically_hosted():
    assert distribution_decision("score.mxl", "CC-BY-4.0", license_scope="file") == ("redistributable", "hosted")
    assert distribution_decision("score.ly", "CC0-1.0", license_scope="file") == ("redistributable", "hosted")
    assert distribution_decision("score.mxl", "CC-BY-NC-4.0", license_scope="file") == ("restricted_or_unclear", "external_link")
    assert distribution_decision("score.mxl", "CC-BY-4.0", license_scope="repository") == ("unverified", "external_link")
    assert distribution_decision("score.mxl", None, license_scope="unknown") == ("unverified", "external_link")


def test_license_url_is_recognized_but_nc_is_not():
    assert canonical_license(None, "https://creativecommons.org/licenses/by/4.0/") == "CC-BY-4.0"
    assert canonical_license(None, "https://creativecommons.org/licenses/by-nc/4.0/") is None


def test_transcriber_credit_is_not_confused_with_composer():
    assert transcriber_credit({"creators": [
        {"name": "Composer", "type": "composer"},
        {"name": "Transcriber", "type": "transcriber"},
    ]}) == "Transcriber"


def test_lilypond_metadata_includes_transcriber_and_license(tmp_path):
    path = tmp_path / "score.ly"
    path.write_text('''% Transcribed by: Ada Transcriber
% License: CC-BY-4.0 https://creativecommons.org/licenses/by/4.0/
\\header {
  title = "Ave verum"
  composer = "W. A. Mozart"
}
''', encoding="utf-8")
    result = analyze_lilypond(path)
    assert result["title"] == "Ave verum"
    assert result["license_spdx"] == "CC-BY-4.0"
    assert transcriber_credit(result) == "Ada Transcriber"
