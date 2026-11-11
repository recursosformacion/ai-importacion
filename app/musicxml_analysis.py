from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_XML_BYTES = 25 * 1024 * 1024


class NotMusicXMLFileError(ValueError):
    """The file is XML, but its document root is not a MusicXML score."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) == name and element.text and element.text.strip():
            return element.text.strip()
    return None


def _xml_payload(path: Path) -> bytes:
    if path.suffix.lower() != ".mxl" and path.stat().st_size > MAX_XML_BYTES:
        raise ValueError(f"El XML supera el límite de análisis de {MAX_XML_BYTES} bytes.")
    if path.suffix.lower() == ".mxl" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            candidates = []
            try:
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                for element in container.iter():
                    if _local_name(element.tag) == "rootfile":
                        name = element.attrib.get("full-path", "")
                        if name and not name.startswith("/") and ".." not in Path(name).parts:
                            candidates.append(name)
            except (KeyError, ET.ParseError):
                pass
            candidates.extend(
                name for name in archive.namelist()
                if name.lower().endswith((".musicxml", ".xml"))
                and name not in candidates
                and not name.startswith("/")
                and ".." not in Path(name).parts
            )
            for name in candidates:
                info = archive.getinfo(name)
                if info.file_size > MAX_XML_BYTES:
                    continue
                payload = archive.read(name)
                try:
                    root = ET.fromstring(payload)
                except ET.ParseError:
                    continue
                if _local_name(root.tag) in {"score-partwise", "score-timewise"}:
                    return payload
        raise ValueError("El archivo comprimido no contiene un MusicXML válido.")
    payload = path.read_bytes()
    if len(payload) > MAX_XML_BYTES:
        raise ValueError(f"El XML supera el límite de análisis de {MAX_XML_BYTES} bytes.")
    return payload


def analyze_musicxml(path: str | Path) -> dict:
    """Extract conservative structural metadata from MusicXML/MXL without altering the source."""
    source = Path(path)
    payload = _xml_payload(source)
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"XML no válido: {exc}") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"score-partwise", "score-timewise"}:
        raise NotMusicXMLFileError("El XML no tiene la estructura de una partitura MusicXML.")
    title = _first_text(root, "movement-title") or _first_text(root, "work-title")
    creators = []
    seen_creators = set()
    for element in root.iter():
        if _local_name(element.tag) == "creator" and element.text and element.text.strip():
            creator = {"name": element.text.strip(), "type": element.attrib.get("type", "")}
            key = (creator["name"].casefold(), creator["type"].casefold())
            if key not in seen_creators:
                seen_creators.add(key)
                creators.append(creator)
    part_names = {}
    for element in root.iter():
        if _local_name(element.tag) == "score-part":
            part_id = element.attrib.get("id", "")
            name = next((child.text.strip() for child in element.iter()
                         if _local_name(child.tag) == "part-name" and child.text and child.text.strip()), "")
            if part_id:
                part_names[part_id] = name
    parts = []
    if root_name == "score-partwise":
        for element in root:
            if _local_name(element.tag) == "part":
                part_id = element.attrib.get("id", "")
                measures = sum(1 for child in element if _local_name(child.tag) == "measure")
                parts.append({"id": part_id, "name": part_names.get(part_id, ""), "measures": measures})
    else:
        measures = sum(1 for child in root if _local_name(child.tag) == "measure")
        parts = [{"id": part_id, "name": name, "measures": measures} for part_id, name in part_names.items()]
    rights_values = [
        element.text.strip() for element in root.iter()
        if _local_name(element.tag) == "rights" and element.text and element.text.strip()
    ]
    license_text = "; ".join(dict.fromkeys(rights_values))
    import re
    license_url_match = re.search(r"https?://[^\\s<>]+", license_text)
    spdx_match = re.search(
        r"\\b(?:CC0-1\\.0|CC-BY-(?:1\\.0|2\\.0|2\\.5|3\\.0|4\\.0))\\b",
        license_text, re.I,
    )
    return {
        "format": "mxl" if source.suffix.lower() == ".mxl" else "musicxml",
        "status": "ok",
        "title": title,
        "creators": creators,
        "license_name": license_text or None,
        "license_spdx": spdx_match.group(0).upper() if spdx_match else None,
        "license_url": license_url_match.group(0).rstrip(".,);]") if license_url_match else None,
        "part_count": len(parts),
        "parts": parts,
        "measure_count": max((part["measures"] for part in parts), default=0),
        "root_element": root_name,
        "source_size_bytes": source.stat().st_size,
    }


def analyze_lilypond(path: str | Path) -> dict:
    """Extract metadata and explicit rights comments from a LilyPond source file."""
    import re

    source = Path(path)
    text = source.read_text("utf-8", errors="replace")
    if len(text) > 25 * 1024 * 1024:
        raise ValueError("El fichero LilyPond supera el límite de análisis.")

    def header_value(key: str) -> str | None:
        match = re.search(
            rf"(?im)^\\s*{re.escape(key)}\\s*=\\s*[\"'](.*?)[\"']",
            text,
        )
        return match.group(1).strip()[:500] if match else None

    title = header_value("title")
    composer = header_value("composer")
    arranger = header_value("arranger")
    transcriber_match = re.search(
        r"(?im)^\\s*%+\\s*(?:transcriber|transcribed by|transcription)\\s*[:=-]\\s*(.+)$",
        text,
    )
    license_match = re.search(
        r"(?im)^\\s*%+\\s*(?:license|licence|rights)\\s*[:=-]\\s*(.+)$",
        text,
    )
    license_text = license_match.group(1).strip()[:1000] if license_match else None
    license_url_match = re.search(r"https?://[^\\s<>]+", license_text or "")
    spdx_match = re.search(
        r"\\b(?:CC0-1\\.0|CC-BY-(?:1\\.0|2\\.0|2\\.5|3\\.0|4\\.0))\\b",
        license_text or "", re.I,
    )
    creators = []
    for name, kind in (
        (composer, "composer"), (arranger, "arranger"),
        (transcriber_match.group(1).strip() if transcriber_match else None, "transcriber"),
    ):
        if name:
            creators.append({"name": name[:500], "type": kind})
    return {
        "format": "lilypond", "status": "ok", "title": title, "creators": creators,
        "license_name": license_text,
        "license_spdx": spdx_match.group(0).upper() if spdx_match else None,
        "license_url": license_url_match.group(0).rstrip(".,);]") if license_url_match else None,
        "source_size_bytes": source.stat().st_size,
    }
