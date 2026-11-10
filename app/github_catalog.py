from __future__ import annotations

import html
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import connect

router = APIRouter()
DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
USER_AGENT = "OSAP-AI-Importacion/0.1 (+https://openmusicrepository.com)"
MUSIC_EXTENSIONS = {
    ".musicxml", ".xml", ".mxl", ".mid", ".midi", ".kar", ".pdf",
    ".krn", ".ly", ".abc", ".mei", ".mscx", ".mscz", ".capx",
    ".sib", ".musx", ".zip",
}
MAX_TREE_ENTRIES = 50000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value: object | None) -> str:
    return html.escape("" if value is None else str(value))


def db():
    # The GitHub catalogue page must fail fast if another worker holds SQLite.
    return connect(DB_PATH, timeout=3)


def ensure_schema() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS github_repositories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          full_name TEXT NOT NULL UNIQUE,
          html_url TEXT NOT NULL,
          default_branch TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          license_spdx TEXT NOT NULL DEFAULT '',
          license_name TEXT NOT NULL DEFAULT '',
          stars INTEGER NOT NULL DEFAULT 0,
          added_at TEXT NOT NULL,
          last_scanned_at TEXT
        );
        CREATE TABLE IF NOT EXISTS github_scans (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repository_id INTEGER NOT NULL REFERENCES github_repositories(id) ON DELETE CASCADE,
          commit_sha TEXT NOT NULL DEFAULT '',
          tree_sha TEXT NOT NULL DEFAULT '',
          file_count INTEGER NOT NULL DEFAULT 0,
          candidate_count INTEGER NOT NULL DEFAULT 0,
          truncated INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          error_text TEXT,
          started_at TEXT NOT NULL,
          completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS github_files (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repository_id INTEGER NOT NULL REFERENCES github_repositories(id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          name TEXT NOT NULL,
          extension TEXT NOT NULL,
          size_bytes INTEGER,
          sha TEXT NOT NULL DEFAULT '',
          blob_url TEXT NOT NULL DEFAULT '',
          html_url TEXT NOT NULL DEFAULT '',
          download_url TEXT NOT NULL DEFAULT '',
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          UNIQUE(repository_id,path)
        );
        CREATE INDEX IF NOT EXISTS idx_github_files_extension ON github_files(extension);
        CREATE INDEX IF NOT EXISTS idx_github_files_repo ON github_files(repository_id,path);
        """)


def parse_repository(value: str) -> tuple[str, str]:
    raw = value.strip()
    if raw.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("La dirección debe pertenecer a github.com.")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
    else:
        parts = [part for part in raw.strip("/").split("/") if part]
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts[:2]):
        raise ValueError("Indica un repositorio como propietario/nombre o su URL de GitHub.")
    owner, name = parts[:2]
    if name.endswith(".git"):
        name = name[:-4]
    return owner, name


def headers() -> dict[str, str]:
    result = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
              "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        result["Authorization"] = "Bearer " + TOKEN
    return result


async def github_json(url: str) -> dict:
    async with httpx.AsyncClient(timeout=45, headers=headers(), follow_redirects=False) as client:
        response = await client.get(url)
        if response.status_code == 404:
            raise ValueError("GitHub no encuentra el repositorio o no es público.")
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            raise ValueError("Se ha alcanzado el límite de consultas de GitHub. Configura GITHUB_TOKEN o inténtalo más tarde.")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("GitHub devolvió una respuesta inesperada.")
    return payload


@router.get("/github", response_class=HTMLResponse)
def github_home():
    try:
        ensure_schema()
        with db() as conn:
            repositories = conn.execute("""
                SELECT r.*, COALESCE((SELECT COUNT(*) FROM github_files f WHERE f.repository_id=r.id),0) AS files,
                       (SELECT COUNT(*) FROM github_scans s WHERE s.repository_id=r.id) AS scans
                FROM github_repositories r ORDER BY COALESCE(r.last_scanned_at,r.added_at) DESC
            """).fetchall()
    except sqlite3.OperationalError as exc:
        # Never leave the browser waiting several minutes on a local DB lock.
        message = "La base de datos local está ocupada o no se puede abrir. La página no ha iniciado ninguna consulta a GitHub. Detalle: " + str(exc)
        return HTMLResponse(_page("Fuentes GitHub no disponible", "<h1>Fuentes GitHub no disponible</h1><p>" + esc(message) + "</p><p><a href='/'>Volver al inicio</a></p>"), status_code=503)
    body = """<h1>Fuentes GitHub</h1>
    <p>Selecciona un repositorio candidato. El procesamiento se realiza localmente y genera un manifiesto JSON descargable; no modifica OSAP.</p>
    <section class="panel"><h2>Añadir repositorio</h2>
    <form method="post" action="/github/add">
      <input name="repository" type="text" required placeholder="OpenScore/Lieder o https://github.com/OpenScore/Lieder">
      <button>Añadir y consultar</button>
    </form>
    <p class="muted">Se admiten repositorios públicos de GitHub. No pegues tokens ni credenciales en este formulario.</p></section>
    <section class="panel"><h2>Repositorios registrados</h2>"""
    if repositories:
        body += "<table><tr><th>Repositorio</th><th>Licencia declarada</th><th>Archivos musicales</th><th>Última lectura</th><th>Acción</th></tr>"
        for repo in repositories:
            license_text = repo["license_spdx"] or repo["license_name"] or "No declarada"
            body += (
                "<tr><td><a href='" + esc(repo["html_url"]) + "'>" + esc(repo["full_name"]) + "</a>"
                "<div class='muted'>" + esc(repo["description"]) + "</div></td>"
                "<td>" + esc(license_text) + "</td><td>" + str(repo["files"]) + "</td>"
                "<td>" + esc(repo["last_scanned_at"] or "Sin explorar") + "</td>"
                "<td><form method='post' action='/github/" + str(repo["id"]) + "/process'><button>Procesar repositorio</button></form>"
                "<a href='/github/" + str(repo["id"]) + "'>Resultado JSON</a></td></tr>"
            )
        body += "</table>"
    else:
        body += "<p>Aún no hay repositorios. Añade uno para empezar.</p>"
    body += "</section><p><a href='/'>Volver al inicio</a> · <a href='/items'>Catálogo general</a></p>"
    return HTMLResponse(_page("Fuentes GitHub", body))


@router.post("/github/add")
async def github_add(repository: str = Form(...)):
    ensure_schema()
    try:
        owner, name = parse_repository(repository)
        metadata = await github_json(f"{API}/repos/{owner}/{name}")
        full_name = metadata.get("full_name")
        if not full_name or metadata.get("private"):
            raise ValueError("Sólo se admiten repositorios públicos.")
        license_data = metadata.get("license") or {}
        with db() as conn:
            conn.execute("""
                INSERT INTO github_repositories
                (full_name,html_url,default_branch,description,license_spdx,license_name,stars,added_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(full_name) DO UPDATE SET html_url=excluded.html_url,
                default_branch=excluded.default_branch,description=excluded.description,
                license_spdx=excluded.license_spdx,license_name=excluded.license_name,stars=excluded.stars
            """, (full_name, metadata.get("html_url", ""), metadata.get("default_branch", "main"),
                  metadata.get("description") or "", license_data.get("spdx_id") or "",
                  license_data.get("name") or "", int(metadata.get("stargazers_count") or 0), now()))
            row = conn.execute("SELECT id FROM github_repositories WHERE full_name=?", (full_name,)).fetchone()
        return RedirectResponse(f"/github/{row['id']}", status_code=303)
    except (ValueError, httpx.HTTPError) as exc:
        return HTMLResponse(_page("No se pudo añadir", "<h1>No se pudo añadir el repositorio</h1><p>" + esc(exc) + "</p><p><a href='/github'>Volver</a></p>"), status_code=400)


@router.post("/github/{repository_id}/scan")
async def github_scan(repository_id: int):
    ensure_schema()
    with db() as conn:
        repo = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
    if not repo:
        raise HTTPException(status_code=404, detail="Repositorio no encontrado")
    started = now()
    try:
        metadata = await github_json(f"{API}/repos/{repo['full_name']}")
        branch = metadata.get("default_branch") or repo["default_branch"]
        tree = await github_json(f"{API}/repos/{repo['full_name']}/git/trees/{branch}?recursive=1")
        entries = tree.get("tree", [])
        if not isinstance(entries, list):
            raise ValueError("GitHub no devolvió un árbol de archivos válido.")
        candidates = []
        for entry in entries[:MAX_TREE_ENTRIES]:
            if entry.get("type") != "blob":
                continue
            path = str(entry.get("path") or "")
            extension = os.path.splitext(path)[1].lower()
            if extension not in MUSIC_EXTENSIONS:
                continue
            candidates.append((path, os.path.basename(path), extension, entry.get("size"),
                               entry.get("sha") or "", entry.get("url") or "",
                               f"https://github.com/{repo['full_name']}/blob/{branch}/" + path))
        stamp = now()
        with db() as conn:
            for path, name, extension, size, sha, blob_url, html_url in candidates:
                conn.execute("""
                    INSERT INTO github_files
                    (repository_id,path,name,extension,size_bytes,sha,blob_url,html_url,download_url,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(repository_id,path) DO UPDATE SET name=excluded.name,
                    extension=excluded.extension,size_bytes=excluded.size_bytes,sha=excluded.sha,
                    blob_url=excluded.blob_url,html_url=excluded.html_url,last_seen_at=excluded.last_seen_at
                """, (repository_id, path, name, extension, size, sha, blob_url, html_url, "", stamp, stamp))
            # Only a complete tree is authoritative enough to remove files no longer present.
            truncated = bool(tree.get("truncated")) or len(entries) > MAX_TREE_ENTRIES
            if not truncated:
                conn.execute(
                    "DELETE FROM github_files WHERE repository_id=? AND last_seen_at<>?",
                    (repository_id, stamp),
                )
            conn.execute("UPDATE github_repositories SET default_branch=?,last_scanned_at=? WHERE id=?",
                         (branch, stamp, repository_id))
            conn.execute("""
                INSERT INTO github_scans(repository_id,commit_sha,tree_sha,file_count,candidate_count,truncated,status,started_at,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (repository_id, "", tree.get("sha") or "",
                  len(entries), len(candidates), int(truncated),
                  "completed", started, stamp))
        return RedirectResponse(f"/github/{repository_id}?scan=ok", status_code=303)
    except (ValueError, httpx.HTTPError) as exc:
        with db() as conn:
            conn.execute("""INSERT INTO github_scans(repository_id,status,error_text,started_at,completed_at)
                            VALUES(?,'error',?,?,?)""", (repository_id, str(exc)[:1000], started, now()))
        return HTMLResponse(_page("Error al leer GitHub", "<h1>No se pudo leer el repositorio</h1><p>" + esc(exc) + "</p><p><a href='/github'>Volver a GitHub</a></p>"), status_code=502)


@router.post("/github/{repository_id}/process")
def github_process(repository_id: int):
    ensure_schema()
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Repositorio no encontrado")
    from app.github_processing import enqueue_repository
    enqueue_repository(repository_id)
    return RedirectResponse(f"/github/{repository_id}", status_code=303)


@router.get("/github/{repository_id}/resources.json")
def github_resources_json(repository_id: int):
    ensure_schema()
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        job = conn.execute("SELECT * FROM github_processing_jobs WHERE repository_id=?", (repository_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Repositorio no encontrado")
    if not job or job["status"] != "completed":
        raise HTTPException(status_code=409, detail="El JSON todavía no está disponible")
    from pathlib import Path
    from fastapi.responses import FileResponse
    path = Path(job["output_dir"]) / "resources.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No se encuentra el JSON generado")
    return FileResponse(path, media_type="application/json", filename=f"github-{repository_id}-resources.json")


@router.get("/github/{repository_id}", response_class=HTMLResponse)
def github_repository(repository_id: int, q: str = ""):
    ensure_schema()
    with db() as conn:
        repo = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        files = conn.execute("""
            SELECT * FROM github_files WHERE repository_id=? AND (?='' OR path LIKE ?)
            ORDER BY path LIMIT 500
        """, (repository_id, q, "%" + q + "%")).fetchall()
        scan = conn.execute("SELECT * FROM github_scans WHERE repository_id=? ORDER BY id DESC LIMIT 1", (repository_id,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM github_files WHERE repository_id=?", (repository_id,)).fetchone()[0]
    body = "<h1>" + esc(repo["full_name"]) + "</h1><p>" + esc(repo["description"]) + "</p>"
    body += "<p><a href='" + esc(repo["html_url"]) + "'>Abrir en GitHub</a> · Licencia declarada: <b>" + esc(repo["license_spdx"] or repo["license_name"] or "No declarada") + "</b></p>"
    from app.github_processing import ensure_processing_schema
    ensure_processing_schema()
    with db() as conn:
        job = conn.execute("SELECT * FROM github_processing_jobs WHERE repository_id=?", (repository_id,)).fetchone()
    if job and job["status"] == "completed":
        body += "<p>Procesamiento terminado. Recursos JSON: " + str(job["processed_count"]) + ".</p><p><a href='/github/" + str(repository_id) + "/resources.json'>Descargar JSON generado</a></p>"
        body += "<form method='post' action='/github/" + str(repository_id) + "/process'><button>Actualizar desde GitHub</button></form>"
    elif job and job["status"] == "running":
        body += "<p>Procesamiento en curso. Si se interrumpió el servidor, puedes reanudarlo; los resultados previos se conservan.</p>"
        body += "<form method='post' action='/github/" + str(repository_id) + "/process'><button>Reanudar procesamiento</button></form>"
    elif job and job["status"] == "error":
        body += "<p>No se pudo completar el procesamiento: " + esc(job["error_text"]) + "</p>"
        body += "<form method='post' action='/github/" + str(repository_id) + "/process'><button>Reintentar</button></form>"
    else:
        body += "<p>El repositorio está seleccionado y preparado para procesarse localmente.</p>"
        body += "<form method='post' action='/github/" + str(repository_id) + "/process'><button>Procesar repositorio</button></form>"
    body += "<p><a href='/github'>Volver a candidatos</a></p>"
    return HTMLResponse(_page("Repositorio " + repo["full_name"], body))


def _page(title: str, body: str) -> str:
    style = """<style>
    body{font:16px system-ui,sans-serif;color:#20242a;background:#f4f6f8;margin:0}
    header{background:#15283f;color:white;padding:1rem max(1rem,calc((100% - 1100px)/2))}
    header a{color:white;margin-right:1.2rem;text-decoration:none}
    main{max-width:1100px;margin:1.5rem auto;padding:0 1rem}
    .panel{background:white;border:1px solid #dce2e8;border-radius:10px;padding:1rem;margin:1rem 0}
    input,button{font:inherit;padding:.65rem;border:1px solid #aab6c4;border-radius:6px}
    button{background:#174f89;color:white;border:0;cursor:pointer}
    table{border-collapse:collapse;width:100%;background:white}td,th{text-align:left;padding:.65rem;border-bottom:1px solid #e1e6eb;vertical-align:top}
    th{background:#edf1f5}.muted{color:#5d6977;font-size:.9rem}a{color:#175ca8}
    </style>"""
    return ("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + esc(title) + "</title>" + style +
            "<header><b><a href='/'>AI Importación</a></b><a href='/items'>Catálogo local</a><a href='/intake'>Captación integrada</a><a href='/github'>Fuentes GitHub</a></header>"
            "<main>" + body + "<p class='muted'>La presencia de un fichero público no acredita derechos de redistribución. El procesamiento local no escribe en OSAP.</p></main></html>")
