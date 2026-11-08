from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "osap-resource-import/1.0"


def stable_resource_id(repository: str, source_path: str) -> str:
    """Stable identifier for one acquired file; it is not an OSAP database ID."""
    value = (repository.strip().casefold() + "\n" + source_path.replace("\\", "/").strip()).encode("utf-8")
    return "github-" + hashlib.sha256(value).hexdigest()[:24]


def build_github_resource_document(
    repository: dict[str, Any],
    source_file: dict[str, Any],
    analysis: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    *,
    package_file: str,
) -> dict[str, Any]:
    """Build one source-neutral import document for one downloaded GitHub file.

    Values are observations from acquisition, not resolved OSAP Work/Person IDs.
    """
    full_name = str(repository.get("full_name") or "")
    source_path = str(source_file.get("path") or "").replace("\\", "/")
    analysis_data = analysis or {}
    extraction_data = extraction or {}
    metadata = analysis_data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    extracted_title = metadata.get("work_title") or metadata.get("title")
    filename_stem = Path(str(source_file.get("name") or source_path)).stem
    instrumentation = metadata.get("instrumentation")
    if instrumentation is None:
        instrumentation = [
            part.get("name")
            for part in (metadata.get("parts") or [])
            if isinstance(part, dict) and part.get("name")
        ]
    return {
        "schema": SCHEMA_VERSION,
        "resource_key": stable_resource_id(full_name, source_path),
        "resource": {
            "title": extracted_title,
            "title_source": metadata.get("title_source") if extracted_title else None,
            "title_candidates": [
                {"value": filename_stem, "source": "source_filename"}
            ] if filename_stem else [],
            "creators": metadata.get("creators") or [],
            "creator_candidates": metadata.get("creator_candidates") or [],
            "instrumentation": instrumentation or [],
            "metadata_status": analysis_data.get("status") or "not_analyzed",
            "metadata_source": "file_analysis" if metadata else None,
        },
        "source": {
            "provider": "github",
            "repository": full_name,
            "repository_url": repository.get("html_url"),
            "repository_description": repository.get("description") or "",
            "default_branch": repository.get("default_branch"),
            "license": {
                "spdx": repository.get("license_spdx") or None,
                "name": repository.get("license_name") or None,
                "scope": "repository_declared_license",
                "verified_for_file": False,
            },
            "file_path": source_path,
            "file_url": source_file.get("html_url"),
            "download_url": source_file.get("download_url"),
            "git_blob_sha": source_file.get("git_sha") or None,
        },
        "file": {
            "package_path": package_file,
            "original_name": source_file.get("name"),
            "format": str(source_file.get("extension") or "").lstrip(".").lower(),
            "size_bytes": source_file.get("downloaded_bytes") or source_file.get("size_bytes"),
            "sha256": source_file.get("sha256"),
            "downloaded_at": source_file.get("downloaded_at"),
        },
        "analysis": {
            "type": analysis_data.get("type"),
            "status": analysis_data.get("status") or "not_analyzed",
            "metadata": metadata,
            "error": analysis_data.get("error"),
            "analyzed_at": analysis_data.get("analyzed_at"),
        },
        "text_extraction": {
            "status": extraction_data.get("status") or "not_extracted",
            "page_count": extraction_data.get("page_count") or 0,
            "character_count": extraction_data.get("character_count") or 0,
            "text": extraction_data.get("text") or "",
            "error": extraction_data.get("error"),
            "extracted_at": extraction_data.get("extracted_at"),
        },
        "acquisition": {
            "provider_record_id": source_file.get("id"),
            "repository_record_id": repository.get("id"),
            "first_seen_at": source_file.get("first_seen_at"),
            "last_seen_at": source_file.get("last_seen_at"),
            "generated_by": "ai-importacion",
        },
        "notes": [
            "Este documento describe un recurso adquirido y un único fichero.",
            "No contiene IDs internos de Work, Person, Source, Resource o File de OSAP.",
            "La licencia indicada es la declarada por el repositorio y no está verificada individualmente para este fichero.",
            "Los metadatos musicales son observaciones del análisis; no implican que la obra o las personas estén identificadas en OSAP.",
        ],
    }


def encode_resource_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
