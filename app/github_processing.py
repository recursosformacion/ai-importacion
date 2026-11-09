from __future__ import annotations

"""Inventorize GitHub repositories and batch-fetch only selected music files.

Repository content is untrusted data: scripts and hooks are never executed,
submodules are not initialized and Git LFS payloads are not fetched.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

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
MAX_GEMINI_FILES = int(os.getenv("AI_IMPORTACION_GEMINI_MAX_FILES", "100"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
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


def _run(args, cwd=None, timeout=1800, input_text=None):
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return subprocess.run(
        args, cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=input_text,
        timeout=timeout, check=True,
    )


def _safe_repo_dir(repository_id: int) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    target = (ROOT / str(int(repository_id))).resolve()
    if target.parent != ROOT:
        raise ValueError("Ruta de repositorio no válida")
    return target


def _prepare_music_snapshot(url: str, repo_dir: Path) -> tuple[str, list[str]]:
    """Fetch the tree first, then materialize all selected music blobs in one Git pack."""
    if repo_dir.exists() and not (repo_dir / ".git").is_dir():
        shutil.rmtree(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    config = repo_dir / ".git" / "config"
    if not config.is_file():
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        _run([
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
            "clone", "--filter=blob:none", "--no-checkout", "--depth=1",
            "--single-branch", "--no-tags", "--no-recurse-submodules", url, str(repo_dir),
        ], timeout=3600)
    else:
        _run([
            "git", "-c", "core.hooksPath=/dev/null", "fetch", "--filter=blob:none",
            "--depth=1", "--no-tags", "--no-recurse-submodules", "origin",
        ], cwd=repo_dir, timeout=3600)
    commit = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=repo_dir).stdout.strip()
    listing = _run(["git", "ls-tree", "-r", "-z", "--name-only", commit], cwd=repo_dir).stdout
    selected = []
    for raw in listing.split("\\0"):
        if not raw or raw.startswith("/") or ".." in Path(raw).parts:
            continue
        if Path(raw).suffix.lower() in MUSIC:
            selected.append(raw)
    selected = sorted(set(selected), key=str.casefold)[:MAX_FILES]
    # One sparse-checkout operation requests all selected blobs together.
    _run(["git", "sparse-checkout", "init", "--no-cone"], cwd=repo_dir)
    _run(
        ["git", "sparse-checkout", "set", "--no-cone", "--stdin"],
        cwd=repo_dir, timeout=1800, input_text="".join(path + "\\n" for path in selected),
    )
    _run(["git", "checkout", "--force", commit], cwd=repo_dir, timeout=1800)
    _run(["git", "clean", "-ffd", "-e", ".osap-import/"], cwd=repo_dir, timeout=600)
    return commit, selected


def _read_secrets_file() -> list[str]:
    """Read local secrets.txt without ever logging or returning its contents in errors."""
    candidates = [
        Path(os.getenv("AI_IMPORTACION_SECRETS", "")).expanduser()
        if os.getenv("AI_IMPORTACION_SECRETS") else None,
        Path(__file__).resolve().parents[1] / "secrets.txt",
        Path.cwd() / "secrets.txt",
    ]
    keys: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if path is None or not path.is_file():
            continue
        try:
            for raw in path.read_text("utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name, value = line.split("=", 1)
                    if name.strip().upper() not in {"GEMINI_API_KEY", "GEMINI_API_KEYS"}:
                        continue
                    line = value.strip().strip('"').strip("'")
                for key in re.split(r"[,;\s]+", line):
                    key = key.strip().strip('"').strip("'")
                    if key and key not in seen and not key.lower().startswith(("your_", "fake_", "replace_")):
                        keys.append(key)
                        seen.add(key)
        except OSError:
            continue
    env_value = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
    for key in re.split(r"[,;\s]+", env_value.strip()):
        key = key.strip()
        if key and key not in seen:
            keys.insert(0, key)
            seen.add(key)
    return keys


def _json_from_model(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("La respuesta de Gemini no es un objeto JSON")
    return value


def _gemini_identify(context: str, path: str, keys: list[str]) -> dict | None:
    if not context.strip() or not keys:
        return None
    prompt = (
        "Identifica los datos bibliográficos de esta partitura o fichero musical. "
        "No inventes información. El nombre del fichero es una pista, no debe "
        "convertirse automáticamente en título. Distingue título de obra, compositor, "
        "arreglista y plantilla instrumental. Si un dato no consta, devuelve null. "
        "Responde exclusivamente con JSON y estas claves: title, composer, arranger, "
        "instrumentation, confidence (0 a 1), notes.\n"
        f"Ruta del fichero: {path}\nContenido o texto extraído:\n{context[:24000]}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    for key in keys:
        try:
            response = httpx.post(
                GEMINI_ENDPOINT.format(model=GEMINI_MODEL),
                params={"key": key},
                json=payload,
                timeout=90,
            )
            response.raise_for_status()
            body = response.json()
            parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            answer = next((part.get("text", "") for part in parts if part.get("text")), "")
            if answer:
                return _json_from_model(answer)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            # Rotate to the next configured key; never expose credentials in diagnostics.
            continue
    return None


def _extract_context(path: Path, limit: int = 24000) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            chunks = []
            for page in reader.pages[:5]:
                chunks.append(page.extract_text() or "")
            return "\n".join(chunks)[:limit]
        if suffix in {".mxl", ".mscz", ".capx"} or zipfile.is_zipfile(path):
            chunks = []
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith((".xml", ".musicxml", ".mei", ".txt")):
                        continue
                    if info.file_size > 8 * 1024 * 1024:
                        continue
                    try:
                        chunks.append(archive.read(info).decode("utf-8", errors="replace"))
                    except (OSError, RuntimeError, zipfile.BadZipFile):
                        continue
                    if sum(map(len, chunks)) >= limit:
                        break
            return "\n".join(chunks)[:limit]
        if suffix in {".musicxml", ".xml", ".mei", ".krn", ".ly", ".abc", ".nwctxt", ".txt"}:
            return path.read_text("utf-8", errors="replace")[:limit]
    except Exception:
        return ""
    return ""


def _musicxml_title(data: bytes) -> str | None:
    try:
        root = ET.fromstring(data[:8 * 1024 * 1024])
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] in {"work-title", "movement-title"}:
                value = (node.text or "").strip()
                if value:
                    return value[:500]
    except Exception:
        return None
    return None


def _explicit_title(path: Path, suffix: str) -> str | None:
    try:
        if suffix in {".musicxml", ".xml"}:
            data = path.read_bytes()
            if b"<score-partwise" in data[:65536].lower() or b"<score-timewise" in data[:65536].lower():
                return _musicxml_title(data)
        elif suffix in {".mxl", ".mscz", ".capx"}:
            context = _extract_context(path, 12000)
            match = re.search(r"<(?:[A-Za-z0-9_-]+:)?(?:work-title|movement-title)\b[^>]*>(.*?)</", context, re.I | re.S)
            if match:
                return re.sub(r"<[^>]+>", "", match.group(1)).strip()[:500] or None
    except OSError:
        return None
    return None


def _relative_file(repo_dir: Path, raw_path: str) -> Path | None:
    candidate = repo_dir / raw_path
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(repo_dir) or not resolved.is_file() or resolved.is_symlink():
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


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
            conn.execute(
                """INSERT INTO github_processing_jobs
                   (repository_id,status,started_at,updated_at,error_text)
                   VALUES(?,'running',?,?,NULL)
                   ON CONFLICT(repository_id) DO UPDATE SET status='running',
                   started_at=excluded.started_at,updated_at=excluded.updated_at,error_text=NULL""",
                (repository_id, started, started),
            )
        url = "https://github.com/" + repo["full_name"] + ".git"
        commit, selected_paths = _prepare_music_snapshot(url, repo_dir)
        paths = []
        for relative in selected_paths:
            path = _relative_file(repo_dir, relative)
            if path is not None:
                paths.append(path)

        out = repo_dir / ".osap-import"
        out.mkdir(exist_ok=True)
        manifest_path = out / "resources.json"
        existing = {}
        if manifest_path.exists():
            try:
                existing = {
                    item["source"]["path"]: item
                    for item in json.loads(manifest_path.read_text("utf-8")).get("resources", [])
                }
            except (OSError, ValueError, KeyError, TypeError):
                existing = {}

        api_keys = _read_secrets_file()
        ai_calls = 0
        records = []
        for path in paths:
            relative = path.relative_to(repo_dir).as_posix()
            try:
                raw_size = path.stat().st_size
                if raw_size > 100 * 1024 * 1024:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            suffix = path.suffix.lower()
            if suffix == ".xml" and not (
                b"<score-partwise" in raw[:65536].lower()
                or b"<score-timewise" in raw[:65536].lower()
            ):
                continue

            previous = existing.get(relative)
            sha = subprocess.run(
                ["git", "hash-object", "--", relative], cwd=repo_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
            ).stdout.strip()
            title = _explicit_title(path, suffix)
            title_source = "musicxml" if title else None
            ai_data = None
            context = ""
            if not title and ai_calls < MAX_GEMINI_FILES and api_keys:
                context = _extract_context(path)
                if context.strip():
                    ai_data = _gemini_identify(context, relative, api_keys)
                    ai_calls += 1
                    if ai_data and isinstance(ai_data.get("title"), str) and ai_data["title"].strip():
                        title = ai_data["title"].strip()[:500]
                        title_source = "gemini"

            if previous and previous.get("source", {}).get("git_blob_sha") == sha:
                # Keep a previously identified title when the source file is unchanged.
                old_resource = previous.get("resource", {})
                if not title and old_resource.get("title"):
                    title = old_resource["title"]
                    title_source = old_resource.get("title_source")
                if not ai_data:
                    ai_data = previous.get("identification")

            resource = {
                "title": title,
                "title_source": title_source,
                "title_candidates": [path.stem] if not title else [],
                "format": suffix.lstrip("."),
            }
            if ai_data:
                for source_key, target_key in (
                    ("composer", "composer"),
                    ("arranger", "arranger"),
                    ("instrumentation", "instrumentation"),
                ):
                    value = ai_data.get(source_key)
                    if isinstance(value, (str, list)) and value:
                        resource[target_key] = value
            records.append({
                "resource_key": "github:" + repo["full_name"] + ":" + sha,
                "resource": resource,
                "source": {
                    "provider": "github",
                    "repository": repo["full_name"],
                    "repository_url": repo["html_url"],
                    "path": relative,
                    "commit_sha": commit,
                    "git_blob_sha": sha,
                    "license_spdx": repo["license_spdx"] or None,
                    "license_name": repo["license_name"] or None,
                    "rights_status": "unverified",
                },
                "identification": {
                    "provider": "gemini" if title_source == "gemini" or ai_data else None,
                    "model": GEMINI_MODEL if ai_data else None,
                    "confidence": ai_data.get("confidence") if ai_data else None,
                    "notes": ai_data.get("notes") if ai_data else None,
                } if ai_data else None,
                "processing": {
                    "status": "identified" if title else "candidate",
                    "content_size": len(raw),
                },
            })

        payload = {
            "schema": "osap-resource-import/1.1",
            "generated_at": _now(),
            "source": {
                "provider": "github",
                "repository": repo["full_name"],
                "commit_sha": commit,
                "snapshot": "filtered_shallow_clone",
                "download_strategy": "single_sparse_checkout_batch",
                "local_repository_path": str(repo_dir),
            },
            "rights_notice": (
                "La licencia declarada por GitHub no ha sido verificada. "
                "La presencia pública no acredita permiso de redistribución."
            ),
            "processing": {
                "files_in_snapshot": None,
                "music_files_considered": len(paths),
                "gemini_calls": ai_calls,
                "gemini_model": GEMINI_MODEL if api_keys else None,
                "gemini_configured": bool(api_keys),
                "max_music_files": MAX_FILES,
                "max_gemini_files": MAX_GEMINI_FILES,
                "git_lfs_payloads_downloaded": False,
                "repository_files_downloaded_individually": False,
            },
            "resources": records,
        }
        temp = manifest_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(manifest_path)
        stamp = _now()
        with connect(DB_PATH) as conn:
            conn.execute(
                """UPDATE github_processing_jobs SET status='completed',commit_sha=?,
                   processed_count=?,output_dir=?,updated_at=?,error_text=NULL
                   WHERE repository_id=?""",
                (commit, len(records), str(out), stamp, repository_id),
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


def enqueue_repository(repository_id: int):
    ensure_processing_schema()
    threading.Thread(
        target=process_repository, args=(repository_id,), daemon=True,
        name=f"github-resource-{repository_id}",
    ).start()
