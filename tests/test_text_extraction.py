import app.main as main


def test_extract_text_candidates_filters_noise_and_keeps_possible_titles():
    text = """TABLE OF CONTENTS
1. Kyrie in D major ........ 12
123
Copyright 1901
Ave Maria
This is a very long OCR line that is probably a paragraph and not a title because it contains far too many words to be useful here
"""
    candidates = main.extract_text_candidates(text)
    values = [value for _, value in candidates]
    assert "Kyrie in D major" in values
    assert "Ave Maria" in values
    assert "123" not in values
    assert "Copyright 1901" not in values
