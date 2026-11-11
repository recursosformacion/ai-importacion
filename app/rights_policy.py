from __future__ import annotations

import re
from pathlib import Path

# Automatic public hosting is intentionally limited to plain CC BY and CC0.
# SA, NC, ND, unknown and repository-level declarations require separate review.
_ALLOWED = {"CC0-1.0", "CC-BY-1.0", "CC-BY-2.0", "CC-BY-2.5", "CC-BY-3.0", "CC-BY-4.0"}


def canonical_license(value: str | None, url: str | None = None) -> str | None:
    raw = (value or "").strip().upper().replace("_", "-")
    match = re.search(r"\b(CC0-1\.0|CC-BY-(?:1\.0|2\.0|2\.5|3\.0|4\.0))\b", raw)
    if match:
        return match.group(1)
    candidate = (url or value or "").strip().lower()
    match = re.search(
        r"creativecommons\.org/(?:licenses/by|publicdomain/zero)/(1\.0|2\.0|2\.5|3\.0|4\.0)",
        candidate,
    )
    if match:
        return "CC0-1.0" if "publicdomain/zero" in candidate else "CC-BY-" + match.group(1)
    return None


def distribution_decision(
    file_name: str,
    license_spdx: str | None = None,
    license_name: str | None = None,
    license_url: str | None = None,
    license_scope: str = "unknown",
) -> tuple[str, str]:
    """Return (rights_status, distribution_mode); fail closed for MXL/LilyPond."""
    if Path(file_name).suffix.lower() not in {".mxl", ".ly"}:
        return "not_applicable", "hosted"
    if license_scope != "file":
        return "unverified", "external_link"
    if canonical_license(license_spdx or license_name, license_url) in _ALLOWED:
        return "redistributable", "hosted"
    return "restricted_or_unclear", "external_link"


def transcriber_credit(analysis: dict | None) -> str:
    if not isinstance(analysis, dict):
        return ""
    values = []
    for creator in analysis.get("creators") or []:
        if not isinstance(creator, dict):
            continue
        kind = str(creator.get("type") or "").strip().casefold()
        name = str(creator.get("name") or "").strip()
        if name and kind in {"transcriber", "transcription", "transcribed by"} and name not in values:
            values.append(name)
    return "; ".join(values)
