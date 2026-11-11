from app.license_policy import HOSTABLE, LINK_ONLY, UNKNOWN, evaluate, license_url


def test_allowed_licenses_are_hostable():
    for spdx in ("cc0-1.0", "CC-BY-4.0", "mit", "GPL-3.0", " apache-2.0 "):
        assert evaluate(spdx) == HOSTABLE


def test_unknown_licenses_are_not_hostable():
    assert evaluate(None) == UNKNOWN
    assert evaluate("") == UNKNOWN
    assert evaluate("NOASSERTION") == UNKNOWN
    assert evaluate("other") == UNKNOWN


def test_known_but_not_allowed_licenses_are_link_only():
    assert evaluate("cc-by-nc-4.0") == LINK_ONLY
    assert evaluate("proprietary") == LINK_ONLY


def test_license_url_is_only_built_for_known_licenses():
    assert license_url("cc-by-4.0") == "https://spdx.org/licenses/CC-BY-4.0.html"
    assert license_url("") is None
    assert license_url("noassertion") is None
