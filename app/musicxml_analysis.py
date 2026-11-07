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
    work_title = _first_text(root, "work-title")
    movement_title = _first_text(root, "movement-title")
    # work-title identifies the composition; movement-title may identify only
    # a movement (e.g. Allegro), so it is a fallback rather than the priority.
    title = work_title or movement_title
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
    rights = []
    for element in root.iter():
        if _local_name(element.tag) == "rights" and element.text and element.text.strip():
            value = element.text.strip()
            if value not in rights:
                rights.append(value)
    source_text = _first_text(root, "source")
    encoding = {
        "software": _first_text(root, "software"),
        "encoding_date": _first_text(root, "encoding-date"),
        "supports": [
            element.attrib.copy()
            for element in root.iter()
            if _local_name(element.tag) == "supports"
        ],
    }
    return {
        "format": "mxl" if source.suffix.lower() == ".mxl" else "musicxml",
        "status": "ok",
        "title": title,
        "work_title": work_title,
        "movement_title": movement_title,
        "title_source": "work-title" if work_title else ("movement-title" if movement_title else None),
        "creators": creators,
        "rights": rights,
        "declared_source": source_text,
        "encoding": encoding,
        "part_count": len(parts),
        "parts": parts,
        "instrumentation": [part["name"] for part in parts if part["name"]],
        "measure_count": max((part["measures"] for part in parts), default=0),
        "root_element": root_name,
        "source_size_bytes": source.stat().st_size,
    }
