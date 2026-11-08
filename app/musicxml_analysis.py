from __future__ import annotations

import re
import zipfile
from difflib import SequenceMatcher
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


def _normal_words(value: str) -> tuple[str, ...]:
    value = value.casefold()
    value = re.sub(r"\b(arranged|arrangement|arr)\.?\s*(by)?\b", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return tuple(value.split())


def _looks_like_filename_title(value: str, filename: str) -> bool:
    words = _normal_words(value)
    file_words = set(_normal_words(Path(filename).stem))
    if len(words) < 2 or not file_words:
        return False
    overlap = sum(1 for word in words if word in file_words)
    return overlap / len(words) >= 0.8


def _same_person(left: str, right: str) -> bool:
    a, b = set(_normal_words(left)), set(_normal_words(right))
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True
    return SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio() >= 0.92


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

    credit_values: dict[str, list[str]] = {}
    untyped_credit_values: list[str] = []
    for credit in (element for element in root.iter() if _local_name(element.tag) == "credit"):
        credit_type = next(
            (child.text.strip().casefold() for child in credit
             if _local_name(child.tag) == "credit-type" and child.text and child.text.strip()),
            "",
        )
        values = [
            child.text.strip()
            for child in credit.iter()
            if _local_name(child.tag) == "credit-words" and child.text and child.text.strip()
        ]
        if credit_type:
            bucket = credit_values.setdefault(credit_type, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
        else:
            for value in values:
                if value not in untyped_credit_values:
                    untyped_credit_values.append(value)

    credit_title = next(iter(credit_values.get("title", [])), None)
    # Untyped page credits are not assigned roles. A title is accepted only
    # when its words substantially match the source filename.
    filename_title = next(
        (value for value in untyped_credit_values
         if _looks_like_filename_title(value, source.name)),
        None,
    )
    title = work_title or credit_title or movement_title or filename_title
    title_source = (
        "work-title" if work_title else
        "credit:title" if credit_title else
        "movement-title" if movement_title else
        "credit:untyped_filename_match" if filename_title else None
    )

    creators: list[dict[str, str]] = []

    def add_creator(name: str, creator_type: str) -> None:
        clean = re.sub(r"\s+", " ", name).strip()
        if not clean:
            return
        for existing in creators:
            if existing["type"].casefold() == creator_type.casefold() and _same_person(existing["name"], clean):
                # Prefer the more informative form (e.g. full name over surname).
                if len(clean) > len(existing["name"]):
                    existing["name"] = clean
                return
        creators.append({"name": clean, "type": creator_type})

    for element in root.iter():
        if _local_name(element.tag) == "creator" and element.text and element.text.strip():
            add_creator(element.text, element.attrib.get("type", ""))

    credit_role_map = {
        "composer": "composer", "arranger": "arranger", "lyricist": "lyricist",
        "poet": "poet", "translator": "translator", "artist": "artist", "editor": "editor",
    }
    creator_candidates: list[dict[str, str | None]] = []
    for credit_type, creator_type in credit_role_map.items():
        for value in credit_values.get(credit_type, []):
            # MuseScore credits can combine several people/roles in one line.
            for line in re.split(r"[\r\n]+", value):
                line = line.strip()
                if not line:
                    continue
                arranger = re.match(r"^(?:arr(?:anged)?\.?|adapt(?:ed)?\.?)\s*(?:by\s*)?(.+)$", line, re.I)
                if arranger:
                    add_creator(arranger.group(1), "arranger")
                else:
                    add_creator(line, creator_type)

    # Preserve untyped credits as candidates, never silently call them composers.
    for value in untyped_credit_values:
        if value == filename_title:
            continue
        clean = re.sub(r"\s+", " ", value).strip()
        if clean and not any(_same_person(clean, item["name"]) for item in creators):
            if not any(_same_person(clean, str(item["name"])) for item in creator_candidates):
                creator_candidates.append({"name": clean, "type": None})

    subtitles = credit_values.get("subtitle", [])
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
        "subtitle": subtitles[0] if subtitles else None,
        "title_source": title_source,
        "creators": creators,
        "creator_candidates": creator_candidates,
        "untyped_credits": untyped_credit_values,
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
