from app.github_acquisition import extract_reference_candidates, resolve_manifest_entries


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


def test_extracts_spanish_attribution_entity_and_url():
    readme = (
        "Les pedimos amablemente que, en cualquier uso público de estas partituras, "
        "mencionen a OpenScore String Quartets y proporcionen un enlace a "
        "[OSSQ o a este repositorio.](https://musescore.com/openscore-string-quartets)"
    )

    references = extract_reference_candidates(readme, "README.md")

    assert len(references) == 1
    assert references[0]["label"] == "OpenScore String Quartets"
    assert references[0]["url"] == "https://musescore.com/openscore-string-quartets"
    assert "uso público" in references[0]["instruction_text"].lower()
    assert references[0]["required"] == 1


def test_does_not_treat_unrelated_readme_links_as_attribution():
    readme = (
        "## Installation\n\n"
        "See [the documentation](https://example.org/docs) for setup."
    )

    assert extract_reference_candidates(readme, "README.md") == []


def test_extracts_plain_url_and_project_name_from_credit_request():
    readme = (
        "We kindly ask that you credit OpenScore Lieder and provide a link to "
        "https://musescore.com/openscore-lieder-corpus for any public-facing use."
    )

    references = extract_reference_candidates(readme, "README.md")

    assert len(references) == 1
    assert references[0]["label"] == "OpenScore Lieder"
    assert references[0]["url"] == "https://musescore.com/openscore-lieder-corpus"



def test_manifest_paths_are_resolved_relative_to_manifest_and_checked_against_tree():
    manifest = [
        {"in": "../scores/Composer/Set/Song/lc123.mscx",
         "out": "../scores/Composer/Set/Song/lc123.mxl"},
        {"in": "../../outside.mscx", "out": "../outside.mxl"},
    ]

    result = resolve_manifest_entries(
        "data/corpus_conversion.json",
        manifest,
        {"scores/Composer/Set/Song/lc123.mscx"},
    )

    assert result["entry_count"] == 2
    assert result["input_paths"] == ["scores/Composer/Set/Song/lc123.mscx"]
    assert result["output_paths"] == ["scores/Composer/Set/Song/lc123.mxl"]
    assert result["missing_input_paths"] == []
    assert result["invalid_path_count"] == 2
