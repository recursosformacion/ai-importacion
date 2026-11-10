from __future__ import annotations

"""Generate the GitHub resource manifest from the acquisition database.

The inventory, file metadata and enrichment results are already persisted by
github_catalog. Manifest generation must therefore be a database export: it
must not clone GitHub again, reread 30k local files or call Gemini.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.db import connect

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
ROOT = Path(os.getenv("AI_IMPORTACION_GITHUB_DIR", "data/github-repositories")).resolve()
_LOCKS: dict[int, threading.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_processing_schema() -> None:
    with connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS github_processing_jobs(
          repository_id INTEGER PRIMARY KEY,
          status TEXT NOT NULL,
          commit_sha TEXT NOT NULL DEFAULT '',
          processed_count INTEGER NOT NULL DEFAULT 0,
          output_dir TEXT NOT NULL DEFAULT '',
          error_text TEXT,
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(repository_id) REFERENCES github_repositories(id) ON DELETE CASCADE
        );
        """)


def _json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _resource_record(repo, row) -> dict:
    metadata = _json_object(row["analysis_json"])
    ai = _json_object(row["ai_metadata_json"]) if row["ai_sha256"] == row["sha256"] else {}

    title = metadata.get("work_title") or metadata.get("title")
    title_source = metadata.get("title_source")
    if not title and ai.get("title"):
        title = ai["title"]
        title_source = "ai:filename_and_metadata"

    creators = list(metadata.get("creators") or [])
    for creator in ai.get("creators") or []:
        if not isinstance(creator, dict) or not creator.get("name"):
            continue
        if not any(
            isinstance(old, dict)
            and str(old.get("type", "")).casefold() == str(creator.get("type", "")).casefold()
            and str(old.get("name", "")).casefold() == str(creator.get("name", "")).casefold()
            for old in creators
        ):
            creators.append({"name": creator["name"], "type": creator.get("type", "unknown")})

    instrumentation = metadata.get("instrumentation")
    if instrumentation is None:
        instrumentation = [
            part.get("name") for part in (metadata.get("parts") or [])
            if isinstance(part, dict) and part.get("name")
        ]

    extension = (row["extension"] or Path(row["path"]).suffix).lower().lstrip(".")
    status = row["analysis_status"] or "not_analyzed"
    return {
        "resource_key": "github:" + repo["full_name"] + ":" + (row["sha"] or row["path"]),
        "resource": {
            "title": title,
            "title_source": title_source,
            "title_candidates": metadata.get("title_candidates") or ([] if title else [Path(row["name"]).stem]),
            "creators": creators,
            "creator_candidates": metadata.get("creator_candidates") or ai.get("creator_candidates") or [],
            "instrumentation": instrumentation or ai.get("instrumentation") or [],
            "format": extension,
            "metadata_status": status,
            "metadata_source": "ai_enrichment" if ai else ("file_analysis" if metadata else None),
        },
        "source": {
            "provider": "github",
            "repository": repo["full_name"],
            "repository_url": repo["html_url"],
            "path": row["path"],
            "file_url": row["html_url"],
            "download_url": row["download_url"],
            "commit_sha": row["sha"],
            "license_spdx": repo["license_spdx"] or None,
            "license_name": repo["license_name"] or None,
            "rights_status": "unverified",
        },
        "identification": {
            "provider": "gemini" if ai else None,
            "model": row["ai_model"] if ai else None,
            "confidence": ai.get("confidence"),
            "notes": ai.get("notes"),
        } if ai else None,
        "processing": {
            "status": "identified" if title else "candidate",
            "analysis_status": status,
            "content_size": row["downloaded_bytes"] or row["size_bytes"] or 0,
            "downloaded_at": row["downloaded_at"],
            "analyzed_at": row["analyzed_at"],
        },
    }


def process_repository(repository_id: int) -> None:
    """Export all already-downloaded DB rows to resources.json using SQL + JSON only."""
    ensure_processing_schema()
    lock = _LOCKS.setdefault(int(repository_id), threading.Lock())
    if not lock.acquire(blocking=False):
        return

    started = _now()
    out = ROOT / str(int(repository_id)) / ".osap-import"
    try:
        with connect(DB_PATH) as conn:
            repo = conn.execute(
                "SELECT * FROM github_repositories WHERE id=?", (repository_id,)
            ).fetchone()
            if repo is None:
                raise ValueError("Repositorio inexistente")

            conn.execute(
                """INSERT INTO github_processing_jobs
                   (repository_id,status,started_at,updated_at,error_text)
                   VALUES(?,'running',?,?,NULL)
                   ON CONFLICT(repository_id) DO UPDATE SET status='running',
                   started_at=excluded.started_at,updated_at=excluded.updated_at,error_text=NULL""",
                (repository_id, started, started),
            )

            rows = conn.execute("""
                SELECT f.id,f.path,f.name,f.extension,f.size_bytes,f.sha,f.html_url,
                       f.download_url,f.downloaded_at,f.downloaded_bytes,f.sha256,
                       a.status AS analysis_status,a.analysis_json,a.error_text AS analysis_error,
                       a.analyzed_at,
                       ai.metadata_json AS ai_metadata_json,ai.sha256 AS ai_sha256,
                       ai.model AS ai_model
                FROM github_files f
                LEFT JOIN github_file_analyses a ON a.github_file_id=f.id
                LEFT JOIN github_ai_metadata_enrichments ai ON ai.github_file_id=f.id
                WHERE f.repository_id=?
                  AND f.download_status='downloaded'
                  AND f.local_path IS NOT NULL
                  AND LOWER(COALESCE(f.extension,'')) <> '.txt'
                ORDER BY f.path
            """, (repository_id,)).fetchall()

            # The DB query above is the inventory. Do not stat/read files or call
            # Git/Gemini here: metadata already lives in the joined tables.
            records = [_resource_record(repo, row) for row in rows]
            latest_scan = conn.execute(
                "SELECT commit_sha FROM github_scans WHERE repository_id=? "
                "ORDER BY id DESC LIMIT 1", (repository_id,)
            ).fetchone()
            commit_sha = (latest_scan["commit_sha"] if latest_scan else "") or ""

        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "osap-resource-import/1.1",
            "generated_at": _now(),
            "source": {
                "provider": "github",
                "repository": repo["full_name"],
                "commit_sha": commit_sha,
                "snapshot": "database_inventory",
                "download_strategy": "previously_acquired_files",
                "local_repository_path": str(ROOT / str(int(repository_id))),
            },
            "rights_notice": (
                "La licencia declarada por GitHub no ha sido verificada. "
                "La presencia pública no acredita permiso de redistribución."
            ),
            "processing": {
                "files_in_database": len(rows),
                "resources_exported": len(records),
                "metadata_source": "database",
                "github_clone_performed": False,
                "files_read_during_export": 0,
                "gemini_calls_during_export": 0,
            },
            "resources": records,
        }
        manifest = out / "resources.json"
        temporary = out / "resources.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(manifest)

        stamp = _now()
        with connect(DB_PATH) as conn:
            conn.execute(
                """UPDATE github_processing_jobs SET status='completed',commit_sha=?,
                   processed_count=?,output_dir=?,updated_at=?,error_text=NULL
                   WHERE repository_id=?""",
                (commit_sha, len(records), str(out), stamp, repository_id),
            )
    except Exception as exc:
        with connect(DB_PATH) as conn:
            conn.execute(
                """UPDATE github_processing_jobs SET status='error',updated_at=?,
                   error_text=? WHERE repository_id=?""",
                (_now(), str(exc)[:2000], repository_id),
            )
    finally:
        lock.release()


def _process_repository_batch(repository_ids: list[int]) -> None:
    for repository_id in dict.fromkeys(int(value) for value in repository_ids):
        process_repository(repository_id)


def enqueue_repositories(repository_ids: list[int]) -> None:
    ensure_processing_schema()
    selected = list(dict.fromkeys(int(value) for value in repository_ids))
    if selected:
        threading.Thread(
            target=_process_repository_batch,
            args=(selected,),
            daemon=True,
            name="github-resource-db-export",
        ).start()


def enqueue_repository(repository_id: int) -> None:
    enqueue_repositories([repository_id])
