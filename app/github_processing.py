from __future__ import annotations

"""Local, resumable processing of a user-selected GitHub music repository.

Third-party repositories are treated as data: no scripts, hooks, submodules,
filters or Git LFS payloads are executed/fetched.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.db import connect

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
ROOT = Path(os.getenv("AI_IMPORTACION_GITHUB_DIR", "data/github-repositories")).resolve()
MUSIC = {
    ".musicxml", ".mxl", ".xml", ".mid", ".midi", ".kar", ".krn", ".ly",
    ".abc", ".mei", ".mscx", ".mscz", ".capx", ".sib", ".musx", ".mus",
    ".ove", ".enc", ".nwc", ".nwctxt", ".gp", ".gp3", ".gp4", ".gp5",
    ".gpx", ".gp7", ".pdf",
}
MAX_FILES = int(os.getenv("AI_IMPORTACION_GITHUB_MAX_FILES", "3000"))
_LOCKS: dict[int, threading.Lock] = {}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_processing_schema():
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


def _run(args, cwd=None, timeout=1800):
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return subprocess.run(args, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=True)


def _safe_repo_dir(repository_id: int) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    target = (ROOT / str(int(repository_id))).resolve()
    if target.parent != ROOT:
        raise ValueError("Ruta de repositorio no válida")
    return target


def _blob_bytes(repo_dir: Path, path: str) -> bytes:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = subprocess.run(["git", "show", "HEAD:" + path], cwd=repo_dir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=True)
    return result.stdout


def _musicxml_title(data: bytes) -> str | None:
    # Metadata-only parse: never trust filenames as work titles.
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(data[:8 * 1024 * 1024])
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] in {"work-title", "movement-title"}:
                value = (node.text or "").strip()
                if value:
                    return value[:500]
    except Exception:
        return None
    return None


def _is_music_xml(data: bytes) -> bool:
    head = data[:65536].lower()
    return b"<score-partwise" in head or b"<score-timewise" in head


def process_repository(repository_id: int):
    ensure_processing_schema()
    lock = _LOCKS.setdefault(repository_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    started = _now()
    repo_dir = _safe_repo_dir(repository_id)
    try:
        with connect(DB_PATH) as conn:
            repo = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
            if not repo:
                raise ValueError("Repositorio inexistente")
            previous = conn.execute("SELECT * FROM github_processing_jobs WHERE repository_id=?",
                                    (repository_id,)).fetchone()
            if previous and previous["status"] == "completed" and previous["commit_sha"]:
                # Skip identical snapshots; a new commit triggers a fresh incremental pass.
                pass
            conn.execute("""INSERT INTO github_processing_jobs
                (repository_id,status,started_at,updated_at,error_text)
                VALUES(?,'running',?,?,NULL)
                ON CONFLICT(repository_id) DO UPDATE SET status='running',
                started_at=excluded.started_at,updated_at=excluded.updated_at,error_text=NULL""",
                (repository_id, started, started))
        url = "https://github.com/" + repo["full_name"] + ".git"
        if not (repo_dir / ".git").is_dir():
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            _run(["git", "-c", "core.hooksPath=/dev/null", "clone",
                  "--filter=blob:none", "--no-checkout", "--depth=1",
                  "--no-tags", "--no-recurse-submodules", url, str(repo_dir)])
        else:
            _run(["git", "-c", "core.hooksPath=/dev/null", "fetch",
                  "--depth=1", "--no-tags", "--no-recurse-submodules", "origin"], cwd=repo_dir)
            _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=repo_dir)
        commit = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
        listing = _run(["git", "ls-tree", "-r", "-l", "-z", "HEAD"], cwd=repo_dir).stdout
        entries = []
        for raw in listing.split("\0"):
            if not raw:
                continue
            try:
                meta, path = raw.split("\t", 1)
                mode, kind, sha, size = meta.split()
                suffix = Path(path).suffix.lower()
                if kind == "blob" and suffix in MUSIC and not path.startswith("/") and ".." not in Path(path).parts:
                    entries.append((path, sha, int(size) if size.isdigit() else None, suffix))
            except ValueError:
                continue
        entries = entries[:MAX_FILES]
        out = repo_dir / ".osap-import"
        out.mkdir(exist_ok=True)
        manifest_path = out / "resources.json"
        existing = {}
        if manifest_path.exists():
            try:
                existing = {x["source"]["path"]: x for x in json.loads(manifest_path.read_text("utf-8")).get("resources", [])}
            except Exception:
                existing = {}
        records = []
        for path, sha, size, suffix in entries:
            old = existing.get(path)
            if old and old.get("source", {}).get("git_blob_sha") == sha:
                records.append(old)
                continue
            # Fetch only selected music blobs. Do not checkout repository files.
            raw = _blob_bytes(repo_dir, path)
            is_musicxml = suffix in {".xml", ".musicxml"} and _is_music_xml(raw)
            if suffix == ".xml" and not is_musicxml:
                continue
            title = _musicxml_title(raw) if is_musicxml else None
            records.append({
                "resource_key": "github:" + repo["full_name"] + ":" + sha,
                "resource": {
                    "title": title,
                    "title_source": "musicxml" if title else None,
                    "title_candidates": [Path(path).stem],
                    "format": suffix.lstrip("."),
                },
                "source": {
                    "provider": "github",
                    "repository": repo["full_name"],
                    "repository_url": repo["html_url"],
                    "path": path,
                    "commit_sha": commit,
                    "git_blob_sha": sha,
                    "license_spdx": repo["license_spdx"] or None,
                    "license_name": repo["license_name"] or None,
                    "rights_status": "unverified",
                },
                "processing": {"status": "candidate", "content_size": len(raw)},
            })
        payload = {
            "schema": "osap-resource-import/1.0",
            "generated_at": _now(),
            "source": {"provider": "github", "repository": repo["full_name"], "commit_sha": commit},
            "rights_notice": "La licencia declarada por GitHub no ha sido verificada. La presencia pública no acredita permiso de redistribución.",
            "resources": records,
        }
        temp = manifest_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(manifest_path)
        stamp = _now()
        with connect(DB_PATH) as conn:
            conn.execute("""UPDATE github_processing_jobs SET status='completed',commit_sha=?,
                processed_count=?,output_dir=?,updated_at=?,error_text=NULL WHERE repository_id=?""",
                (commit, len(records), str(out), stamp, repository_id))
    except Exception as exc:
        with connect(DB_PATH) as conn:
            conn.execute("""UPDATE github_processing_jobs SET status='error',updated_at=?,
                error_text=? WHERE repository_id=?""", (_now(), str(exc)[:2000], repository_id))
    finally:
        lock.release()


def enqueue_repository(repository_id: int):
    ensure_processing_schema()
    threading.Thread(target=process_repository, args=(repository_id,), daemon=True,
                     name=f"github-resource-{repository_id}").start()
