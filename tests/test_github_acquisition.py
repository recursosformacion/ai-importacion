from app.github_acquisition import extract_reference_candidates


def test_extracts_public_attribution_and_named_project_link():
    readme = (
        "For public use:\n\n"
        "We kindly ask that, in any public use of these scores, you mention "
        "[OpenScore String Quartets](https://musescore.com/openscore-string-quartets)."
    )

    references = extract_reference_candidates(readme, "README.md")

    assert len(references) == 1
    reference = references[0]
    assert reference["reference_type"] == "attribution"
    assert reference["label"] == "OpenScore String Quartets"
    assert reference["url"] == "https://musescore.com/openscore-string-quartets"
    assert "public use" in reference["instruction_text"].lower()
    assert reference["source_path"] == "README.md"
    assert reference["required"] == 1


def test_does_not_treat_unrelated_readme_links_as_attribution():
    readme = (
        "## Installation\n\n"
        "See [the documentation](https://example.org/docs) for setup."
    )

    assert extract_reference_candidates(readme, "README.md") == []
