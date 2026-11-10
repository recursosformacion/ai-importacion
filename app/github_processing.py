from __future__ import annotations

"""Acquire, analyze and export one GitHub repository into resources.json.

Inventory and acquisition are resumable. Selected blobs are fetched together
with a partial sparse Git checkout; structural analysis and Gemini enrichment
are persisted before the final manifest is serialized from SQLite.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.db import DOWNLOAD_DIR, MAX_DOWNLOAD_BYTES, connect

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
ROOT = Path(os.getenv("AI_IMPORTACION_GITHUB_DIR", "data/github-repositories")).resolve()
_LOCKS: dict[int, threading.Lock] = {}
_PROCESSING_QUEUE_LOCK = threading.Lock()


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
          stage TEXT NOT NULL DEFAULT 'pending',
          progress_count INTEGER NOT NULL DEFAULT 0,
          total_count INTEGER NOT NULL DEFAULT 0,
          current_path TEXT NOT NULL DEFAULT '',
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(repository_id) REFERENCES github_repositories(id) ON DELETE CASCADE
        );
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_processing_jobs)")}
        for name, definition in {
            "stage": "TEXT NOT NULL DEFAULT 'pending'",
            "progress_count": "INTEGER NOT NULL DEFAULT 0",
            "total_count": "INTEGER NOT NULL DEFAULT 0",
            "current_path": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE github_processing_jobs ADD COLUMN {name} {definition}")


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
    status = row["analysis_status"] or row["extraction_status"] or "not_analyzed"
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
            "analyzed_at": row["analyzed_at"] or row["extracted_at"],
        },
    }


def _clone_sparse_checkout(clone_url: str, branch: str, paths: list[str],
                         checkout: Path, environment: dict | None = None) -> None:
    """Clone Git metadata once and materialize only the selected repository paths."""
    env = dict(os.environ) if environment is None else environment
    clone = subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", "--depth=1",
         "--no-recurse-submodules", "--single-branch", "--branch", branch,
         clone_url, str(checkout)],
        capture_output=True, text=True, env=env,
    )
    if clone.returncode:
        detail = (clone.stderr or clone.stdout or "git clone falló")[-3000:]
        raise RuntimeError("No se pudo obtener el repositorio mediante Git: " + detail)

    init = subprocess.run(
        ["git", "-C", str(checkout), "sparse-checkout", "init", "--no-cone"],
        capture_output=True, text=True, env=env,
    )
    if init.returncode:
        raise RuntimeError("No se pudo preparar sparse checkout: " + (init.stderr or "")[-2000:])

    patterns = []
    for raw_path in paths:
        path = str(raw_path).replace("\\", "/")
        if not path or path.startswith("/") or "\n" in path or "\r" in path:
            raise ValueError("Ruta de fichero no válida")
        parts = Path(path).parts
        if ".." in parts:
            raise ValueError("Ruta de fichero no válida")
        escaped = "".join("\\" + char if char in "*?[" else char for char in path)
        patterns.append("/" + escaped)

    sparse = subprocess.run(
        ["git", "-C", str(checkout), "sparse-checkout", "set", "--no-cone", "--stdin"],
        input="\n".join(patterns) + "\n",
        capture_output=True, text=True, env=env,
    )
    if sparse.returncode:
        raise RuntimeError("No se pudieron seleccionar los ficheros del repositorio: " +
                           (sparse.stderr or "")[-3000:])

    materialize = subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--force", "HEAD"],
        capture_output=True, text=True, env=env,
    )
    if materialize.returncode:
        raise RuntimeError("No se pudieron materializar los ficheros seleccionados: " +
                           (materialize.stderr or "")[-3000:])


def _ensure_catalog_inventory_and_downloads(repository_id: int) -> bool:
    """Inventory once, then acquire selected blobs in one sparse Git checkout."""
    from app import github_catalog

    with connect(DB_PATH) as conn:
        repo = conn.execute(
            "SELECT * FROM github_repositories WHERE id=?", (repository_id,)
        ).fetchone()
        if repo is None:
            raise ValueError("Repositorio inexistente")
        inventory_count = conn.execute(
            "SELECT COUNT(*) FROM github_files WHERE repository_id=?", (repository_id,)
        ).fetchone()[0]

    if not inventory_count:
        response = asyncio.run(github_catalog.github_scan(repository_id))
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError("No se pudo inventariar el repositorio en GitHub")
        with connect(DB_PATH) as conn:
            repo = conn.execute(
                "SELECT * FROM github_repositories WHERE id=?", (repository_id,)
            ).fetchone()

    with connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT id,path,size_bytes FROM github_files
            WHERE repository_id=? AND extension<>'.txt'
              AND COALESCE(download_status,'not_downloaded')
                  NOT IN ('downloaded','too_large')
            ORDER BY path
        """, (repository_id,)).fetchall()

    pending = []
    for row in rows:
        if row["size_bytes"] is not None and int(row["size_bytes"]) > MAX_DOWNLOAD_BYTES:
            with connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE github_files SET download_status='too_large',download_error=? WHERE id=?",
                    ("Supera el límite configurado", row["id"]),
                )
        elif row["path"] and "\n" not in row["path"] and "\r" not in row["path"]:
            pending.append(row)

    if not pending:
        return False

    full_name = repo["full_name"]
    owner, name = full_name.split("/", 1)
    branch = repo["default_branch"] or "main"
    stamp = _now()
    with connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO github_download_jobs
              (repository_id,status,total,completed,failed,current_path,started_at,updated_at)
            VALUES(?,'running',?,0,0,'',?,?)
            ON CONFLICT(repository_id) DO UPDATE SET status='running',
              total=excluded.total,completed=0,failed=0,current_path='',
              started_at=excluded.started_at,updated_at=excluded.updated_at
        """, (repository_id, len(pending), stamp, stamp))
        conn.execute("""
            UPDATE github_processing_jobs
            SET stage='download',progress_count=0,total_count=?,current_path='',updated_at=?
            WHERE repository_id=?
        """, (len(pending), stamp, repository_id))

    destination_root = DOWNLOAD_DIR / "github" / str(repository_id)
    destination_root.mkdir(parents=True, exist_ok=True)
    clone_url = f"https://github.com/{owner}/{name}.git"
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"

    with tempfile.TemporaryDirectory(prefix="ai-importacion-github-") as temporary:
        checkout = Path(temporary) / "repository"
        _clone_sparse_checkout(
            clone_url, branch, [str(row["path"]) for row in pending],
            checkout, environment,
        )

        completed = 0
        failed = 0
        for row in pending:
            relative = Path(str(row["path"]))
            try:
                if relative.is_absolute() or ".." in relative.parts or "\\" in str(row["path"]):
                    raise ValueError("Ruta de fichero no válida")
                source = checkout.joinpath(*relative.parts)
                destination = destination_root.joinpath(*relative.parts)
                if not source.is_file():
                    raise FileNotFoundError("Git no materializó el fichero seleccionado")
                size = source.stat().st_size
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Supera el límite configurado")
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_file = destination.with_name(destination.name + ".part")
                digest = hashlib.sha256()
                with source.open("rb") as input_file, temporary_file.open("wb") as output_file:
                    while True:
                        chunk = input_file.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        output_file.write(chunk)
                os.replace(temporary_file, destination)
                raw_url = (
                    "https://raw.githubusercontent.com/" + quote(owner, safe="") + "/"
                    + quote(name, safe="") + "/" + quote(branch, safe="") + "/"
                    + quote(str(row["path"]), safe="/")
                )
                with connect(DB_PATH) as conn:
                    conn.execute("""
                        UPDATE github_files SET download_status='downloaded',local_path=?,
                          download_url=?,sha256=?,downloaded_bytes=?,downloaded_at=?,download_error=NULL
                        WHERE id=?
                    """, (str(destination), raw_url, digest.hexdigest(), size, _now(), row["id"]))
                completed += 1
            except Exception as exc:
                failed += 1
                with connect(DB_PATH) as conn:
                    conn.execute("""
                        UPDATE github_files SET download_status='error',download_error=?
                        WHERE id=?
                    """, (str(exc)[:1000], row["id"]))
            with connect(DB_PATH) as conn:
                conn.execute("""
                    UPDATE github_download_jobs SET completed=?,failed=?,current_path=?,updated_at=?
                    WHERE repository_id=?
                """, (completed + failed, failed, str(row["path"]), _now(), repository_id))
                conn.execute("""
                    UPDATE github_processing_jobs SET progress_count=?,current_path=?,updated_at=?
                    WHERE repository_id=?
                """, (completed + failed, str(row["path"]), _now(), repository_id))

    with connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE github_download_jobs SET status='completed',current_path='',updated_at=?
            WHERE repository_id=?
        """, (_now(), repository_id))



def _analyze_downloaded_files(repository_id: int, repository: dict) -> tuple[int, int]:
    """Persist structural analysis, local text extraction and Gemini metadata."""
    from app.analysis_engine import analyze_local_music_file
    from app.content_extraction import SUPPORTED_SUFFIXES, extract_local_content
    from app.github_catalog import ANALYZER_VERSION
    from app.metadata_enrichment import enrich_music_metadata

    structural = {".musicxml", ".mxl", ".xml", ".mid", ".midi", ".kar"}
    with connect(DB_PATH) as conn:
        files = conn.execute("""
            SELECT id,path,name,extension,local_path,sha256
            FROM github_files
            WHERE repository_id=? AND download_status='downloaded'
              AND local_path IS NOT NULL
            ORDER BY path
        """, (repository_id,)).fetchall()
    notation_files = [row for row in files if (row["extension"] or "").lower() in SUPPORTED_SUFFIXES]
    structural_files = [row for row in files if (row["extension"] or "").lower() in structural]
    total = len(notation_files) + len(structural_files)
    completed = 0
    with connect(DB_PATH) as conn:
        conn.execute("""UPDATE github_processing_jobs SET stage='analysis',progress_count=0,
            total_count=?,current_path='',updated_at=? WHERE repository_id=?""", (total, _now(), repository_id))

    # Extract text from PDF, scanned scores and native notation formats locally.
    for item in notation_files:
        with connect(DB_PATH) as conn:
            conn.execute("""UPDATE github_processing_jobs SET progress_count=?,current_path=?,updated_at=?
                WHERE repository_id=?""", (completed, item["path"], _now(), repository_id))
        if completed % 100 == 0:
            logging.getLogger(__name__).info(
                "GitHub %s: análisis local %s/%s (%s)",
                repository_id, completed, total, item["path"],
            )
        with connect(DB_PATH) as conn:
            previous = conn.execute(
                "SELECT status,sha256,extracted_text FROM github_content_extractions WHERE github_file_id=?",
                (item["id"],),
            ).fetchone()
        if previous and previous["sha256"] == item["sha256"] and previous["status"] not in {
            "error", "ocr_error", "ocr_unavailable", "image_ocr_no_text"
        }:
            extracted_text = previous["extracted_text"] or ""
        else:
            result = extract_local_content(item["local_path"])
            extracted_text = result.get("text") or ""
            with connect(DB_PATH) as conn:
                conn.execute("""
                    INSERT INTO github_content_extractions
                      (github_file_id,status,page_count,extracted_characters,extracted_text,error_text,analyzed_at,sha256)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(github_file_id) DO UPDATE SET
                      status=excluded.status,page_count=excluded.page_count,
                      extracted_characters=excluded.extracted_characters,
                      extracted_text=excluded.extracted_text,error_text=excluded.error_text,
                      analyzed_at=excluded.analyzed_at,sha256=excluded.sha256
                """, (item["id"], result["status"], result["page_count"],
                      result["extracted_characters"], extracted_text[:200000],
                      result["error_text"], _now(), item["sha256"]))
        ai_context = {"source_text_excerpt": extracted_text[:3500]} if extracted_text else {}
        with connect(DB_PATH) as conn:
            ai_row = conn.execute(
                "SELECT sha256 FROM github_ai_metadata_enrichments WHERE github_file_id=?",
                (item["id"],),
            ).fetchone()
        if (not ai_row or ai_row["sha256"] != item["sha256"]) and (extracted_text or item["name"]):
            ai = enrich_music_metadata(filename=item["name"], repository=repository, metadata=ai_context)
            if ai.get("status") == "ok":
                with connect(DB_PATH) as conn:
                    conn.execute("""
                        INSERT INTO github_ai_metadata_enrichments
                          (github_file_id,sha256,metadata_json,model,analyzed_at,error_text)
                        VALUES(?,?,?,?,?,NULL)
                        ON CONFLICT(github_file_id) DO UPDATE SET
                          sha256=excluded.sha256,metadata_json=excluded.metadata_json,
                          model=excluded.model,analyzed_at=excluded.analyzed_at,error_text=NULL
                    """, (item["id"], item["sha256"] or "",
                          json.dumps(ai, ensure_ascii=False), ai.get("model") or "", _now()))
        completed += 1
        with connect(DB_PATH) as conn:
            conn.execute("UPDATE github_processing_jobs SET progress_count=?,current_path='',updated_at=? WHERE repository_id=?",
                         (completed, _now(), repository_id))

    # Parse MusicXML/MXL and MIDI structure; reuse results when the content hash is unchanged.
    for item in structural_files:
        with connect(DB_PATH) as conn:
            conn.execute("""UPDATE github_processing_jobs SET progress_count=?,current_path=?,updated_at=?
                WHERE repository_id=?""", (completed, item["path"], _now(), repository_id))
        if completed % 100 == 0:
            logging.getLogger(__name__).info(
                "GitHub %s: análisis estructural %s/%s (%s)",
                repository_id, completed, total, item["path"],
            )
        with connect(DB_PATH) as conn:
            previous = conn.execute(
                "SELECT status,sha256,analyzer_version,analysis_json FROM github_file_analyses WHERE github_file_id=?",
                (item["id"],),
            ).fetchone()
        current = bool(previous and previous["sha256"] == item["sha256"]
                       and previous["analyzer_version"] == ANALYZER_VERSION
                       and previous["status"] in {"ok", "not_musicxml"})
        if not current:
            result = analyze_local_music_file(item["local_path"])
            with connect(DB_PATH) as conn:
                conn.execute("""
                    INSERT INTO github_file_analyses
                      (github_file_id,analysis_type,status,analysis_json,error_text,analyzed_at,sha256,analyzer_version)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(github_file_id) DO UPDATE SET
                      analysis_type=excluded.analysis_type,status=excluded.status,
                      analysis_json=excluded.analysis_json,error_text=excluded.error_text,
                      analyzed_at=excluded.analyzed_at,sha256=excluded.sha256,
                      analyzer_version=excluded.analyzer_version
                """, (item["id"], result["analysis_type"], result["status"],
                      json.dumps(result["analysis"], ensure_ascii=False),
                      result["error_text"], _now(), item["sha256"], ANALYZER_VERSION))
            metadata = result["analysis"] if isinstance(result["analysis"], dict) else {}
        else:
            try:
                metadata = json.loads(previous["analysis_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
        if not (metadata.get("work_title") or metadata.get("title")) or not metadata.get("creators"):
            with connect(DB_PATH) as conn:
                ai_row = conn.execute(
                    "SELECT sha256 FROM github_ai_metadata_enrichments WHERE github_file_id=?",
                    (item["id"],),
                ).fetchone()
            if not ai_row or ai_row["sha256"] != item["sha256"]:
                ai = enrich_music_metadata(filename=item["name"], repository=repository, metadata=metadata)
                if ai.get("status") == "ok":
                    with connect(DB_PATH) as conn:
                        conn.execute("""
                            INSERT INTO github_ai_metadata_enrichments
                              (github_file_id,sha256,metadata_json,model,analyzed_at,error_text)
                            VALUES(?,?,?,?,?,NULL)
                            ON CONFLICT(github_file_id) DO UPDATE SET
                              sha256=excluded.sha256,metadata_json=excluded.metadata_json,
                              model=excluded.model,analyzed_at=excluded.analyzed_at,error_text=NULL
                        """, (item["id"], item["sha256"] or "",
                              json.dumps(ai, ensure_ascii=False), ai.get("model") or "", _now()))
        completed += 1
        with connect(DB_PATH) as conn:
            conn.execute("UPDATE github_processing_jobs SET progress_count=?,updated_at=? WHERE repository_id=?",
                         (completed, _now(), repository_id))
    return completed, total



def process_repository(repository_id: int) -> None:
    """Run acquisition and analysis once, then export the persisted catalog to JSON."""
    ensure_processing_schema()
    lock = _LOCKS.setdefault(int(repository_id), threading.Lock())
    if not lock.acquire(blocking=False):
        return

    started = _now()
    out = ROOT / str(int(repository_id)) / ".osap-import"
    try:
        with connect(DB_PATH) as conn:
            conn.execute("""INSERT INTO github_processing_jobs
                (repository_id,status,stage,progress_count,total_count,started_at,updated_at,error_text)
                VALUES(?,'running','inventory',0,0,?,?,NULL)
                ON CONFLICT(repository_id) DO UPDATE SET status='running',stage='inventory',
                progress_count=0,total_count=0,started_at=excluded.started_at,
                updated_at=excluded.updated_at,error_text=NULL""",
                (repository_id, started, started))
        logging.getLogger(__name__).info("GitHub %s: comienza procesamiento del repositorio", repository_id)
        acquisition_performed = _ensure_catalog_inventory_and_downloads(repository_id)
        with connect(DB_PATH) as conn:
            repo_for_analysis = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
            if repo_for_analysis is None:
                raise ValueError("Repositorio inexistente")
        _analyze_downloaded_files(repository_id, dict(repo_for_analysis))
        with connect(DB_PATH) as conn:
            repo = conn.execute(
                "SELECT * FROM github_repositories WHERE id=?", (repository_id,)
            ).fetchone()
            if repo is None:
                raise ValueError("Repositorio inexistente")

            conn.execute("UPDATE github_processing_jobs SET stage='export',updated_at=? WHERE repository_id=?",
                         (_now(), repository_id))

            inventory_count = conn.execute(
                "SELECT COUNT(*) FROM github_files WHERE repository_id=? AND extension<>'.txt'",
                (repository_id,),
            ).fetchone()[0]
            rows = conn.execute("""
                SELECT f.id,f.path,f.name,f.extension,f.size_bytes,f.sha,f.html_url,
                       f.download_url,f.downloaded_at,f.downloaded_bytes,f.sha256,
                       a.status AS analysis_status,a.analysis_json,a.error_text AS analysis_error,
                       a.analyzed_at,x.status AS extraction_status,x.analyzed_at AS extracted_at,
                       ai.metadata_json AS ai_metadata_json,ai.sha256 AS ai_sha256,
                       ai.model AS ai_model
                FROM github_files f
                LEFT JOIN github_file_analyses a ON a.github_file_id=f.id
                LEFT JOIN github_content_extractions x ON x.github_file_id=f.id
                LEFT JOIN github_ai_metadata_enrichments ai ON ai.github_file_id=f.id
                WHERE f.repository_id=?
                  AND f.download_status='downloaded'
                  AND f.local_path IS NOT NULL
                  AND LOWER(COALESCE(f.extension,'')) <> '.txt'
                  AND NOT (LOWER(COALESCE(f.extension,''))='.xml' AND a.status='not_musicxml')
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
                "download_strategy": "sparse_git_checkout" if acquisition_performed else "reused_local_inventory",
                "local_repository_path": str(DOWNLOAD_DIR / "github" / str(int(repository_id))),
            },
            "rights_notice": (
                "La licencia declarada por GitHub no ha sido verificada. "
                "La presencia pública no acredita permiso de redistribución."
            ),
            "processing": {
                "inventory_files_in_database": inventory_count,
                "downloaded_files_in_database": len(rows),
                "resources_exported": len(records),
                "not_exported_or_not_downloaded": max(0, inventory_count - len(rows)),
                "metadata_source": "database",
                "github_clone_performed": acquisition_performed,
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
                """UPDATE github_processing_jobs SET status='completed',stage='completed',commit_sha=?,
                   processed_count=?,output_dir=?,progress_count=?,total_count=?,current_path='',updated_at=?,error_text=NULL
                   WHERE repository_id=?""",
                (commit_sha, len(records), str(out), len(records), len(records), stamp, repository_id),
            )
    except Exception as exc:
        logging.getLogger(__name__).exception("GitHub %s: error durante el procesamiento", repository_id)
        with connect(DB_PATH) as conn:
            conn.execute(
                """UPDATE github_processing_jobs SET status='error',stage='error',current_path='',updated_at=?,
                   error_text=? WHERE repository_id=?""",
                (_now(), str(exc)[:2000], repository_id),
            )
    finally:
        logging.getLogger(__name__).info("GitHub %s: procesamiento finalizado", repository_id)
        lock.release()


def _process_repository_batch(repository_ids: list[int]) -> None:
    # A single queue worker at a time prevents simultaneous clones and Gemini
    # batches when the user selects several repositories from the same page.
    with _PROCESSING_QUEUE_LOCK:
        for repository_id in dict.fromkeys(int(value) for value in repository_ids):
            process_repository(repository_id)


def recover_processing_jobs() -> int:
    """Resume jobs interrupted by a server restart or a lost daemon thread."""
    ensure_processing_schema()
    with connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT repository_id FROM github_processing_jobs WHERE status IN ('queued','running','pending') ORDER BY updated_at"
        ).fetchall()
        ids = [int(row["repository_id"]) for row in rows]
        if ids:
            conn.execute("""
                UPDATE github_processing_jobs
                SET status='queued',stage='queued',current_path='',updated_at=?,error_text=NULL
                WHERE status IN ('queued','running','pending')
            """, (_now(),))
    if ids:
        logging.getLogger(__name__).warning(
            "Recuperando %s trabajos GitHub pendientes/interrumpidos", len(ids)
        )
        enqueue_repositories(ids)
    return len(ids)


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
