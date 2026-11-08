from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import tempfile
import zipfile
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from app.db import DOWNLOAD_DIR, MAX_DOWNLOAD_BYTES, connect
from app.resource_export import build_github_resource_document, encode_resource_json, stable_resource_id

router = APIRouter()
_DOWNLOAD_TASKS: dict[int, asyncio.Task] = {}
DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
USER_AGENT = "OSAP-AI-Importacion/0.1 (+https://openmusicrepository.com)"
# Discovery inventory: notation, native score-editor files, audio and containers.
# Not every inventoried format has a structural analyzer yet; unknown internals
# remain downloadable candidates rather than being silently discarded.
MUSIC_EXTENSIONS = {
    # Interchange and text notation
    ".musicxml", ".mxl", ".xml", ".mid", ".midi", ".kar", ".krn",
    ".ly", ".ily", ".abc", ".mei", ".mml",
    # Score editors and notation applications
    ".mscx", ".mscz", ".capx", ".sib", ".musx", ".gp", ".gp3",
    ".gp4", ".gp5", ".gpx", ".ptb", ".nwc", ".nwctxt", ".nwz",
    ".bww", ".ove", ".enc", ".mgu", ".sgu", ".not",
    # Audio
    ".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac",
    ".aiff", ".aif", ".wma", ".ape", ".alac",
    # Scanned scores and containers
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".zip",
}
MAX_TREE_ENTRIES = 50000
# Increment when extraction rules change so cached metadata is recalculated.
ANALYZER_VERSION = "musicxml-credits-4"

# Only explicit music/notation/audio formats count towards the repository threshold.
# Generic documents, images and archives are not evidence of a musical piece.
MUSICAL_PIECE_EXTENSIONS = MUSIC_EXTENSIONS - {
    ".xml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".zip",
}
MIN_MUSICAL_PIECES = 11
MAX_REPOSITORIES_TO_INSPECT = 30


async def count_musical_pieces(repository: dict) -> tuple[int, bool]:
    """Return recognized musical files in a repository tree and truncation state."""
    full_name = repository.get("full_name") or ""
    branch = repository.get("default_branch") or "main"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
        return 0, False
    tree = await github_json(
        f"{API}/repos/{full_name}/git/trees/{quote(branch, safe='')}?recursive=1"
    )
    entries = tree.get("tree") or []
    count = 0
    for entry in entries[:MAX_TREE_ENTRIES]:
        if entry.get("type") != "blob":
            continue
        extension = os.path.splitext(str(entry.get("path") or ""))[1].lower()
        if extension in MUSICAL_PIECE_EXTENSIONS:
            count += 1
    return count, bool(tree.get("truncated")) or len(entries) > MAX_TREE_ENTRIES




def fetch_repository_readme(full_name: str) -> str:
    """Read the repository README once as contextual evidence for metadata enrichment."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name or ""):
        return ""
    try:
        response = httpx.get(
            f"{API}/repos/{full_name}/readme",
            headers={**headers(), "Accept": "application/vnd.github+json"},
            timeout=20,
        )
        if response.status_code != 200:
            return ""
        payload = response.json()
        content = payload.get("content") or ""
        if payload.get("encoding") == "base64":
            raw = base64.b64decode(content)
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = str(content)
        return text[:12000]
    except Exception:
        return ""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value: object | None) -> str:
    return html.escape("" if value is None else str(value))


def db():
    return connect(DB_PATH)


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
          downloaded_at TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          UNIQUE(repository_id,path)
        );
        CREATE TABLE IF NOT EXISTS github_file_analyses (
          github_file_id INTEGER PRIMARY KEY REFERENCES github_files(id) ON DELETE CASCADE,
          analysis_type TEXT NOT NULL,
          status TEXT NOT NULL,
          analysis_json TEXT NOT NULL,
          error_text TEXT,
          analyzed_at TEXT NOT NULL,
          sha256 TEXT,
          analyzer_version TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS github_content_extractions (
          github_file_id INTEGER PRIMARY KEY REFERENCES github_files(id) ON DELETE CASCADE,
          status TEXT NOT NULL,
          page_count INTEGER NOT NULL DEFAULT 0,
          extracted_characters INTEGER NOT NULL DEFAULT 0,
          extracted_text TEXT NOT NULL DEFAULT '',
          error_text TEXT,
          analyzed_at TEXT NOT NULL,
          sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS github_ai_metadata_enrichments (
          github_file_id INTEGER PRIMARY KEY REFERENCES github_files(id) ON DELETE CASCADE,
          sha256 TEXT NOT NULL DEFAULT '',
          metadata_json TEXT NOT NULL,
          model TEXT NOT NULL DEFAULT '',
          analyzed_at TEXT NOT NULL,
          error_text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_github_files_extension ON github_files(extension);
        CREATE INDEX IF NOT EXISTS idx_github_files_repo ON github_files(repository_id,path);
        CREATE TABLE IF NOT EXISTS github_download_jobs (
          repository_id INTEGER PRIMARY KEY REFERENCES github_repositories(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'idle',
          total INTEGER NOT NULL DEFAULT 0,
          completed INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          current_path TEXT NOT NULL DEFAULT '',
          started_at TEXT,
          updated_at TEXT
        );
        """)
        repository_columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_repositories)")}
        if "readme_text" not in repository_columns:
            conn.execute("ALTER TABLE github_repositories ADD COLUMN readme_text TEXT NOT NULL DEFAULT ''")
        analysis_columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_file_analyses)")}
        if "analyzer_version" not in analysis_columns:
            conn.execute("ALTER TABLE github_file_analyses ADD COLUMN analyzer_version TEXT NOT NULL DEFAULT ''")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_files)")}
        additions = {
            "download_status": "TEXT NOT NULL DEFAULT 'not_downloaded'",
            "local_path": "TEXT",
            "sha256": "TEXT",
            "downloaded_bytes": "INTEGER",
            "download_error": "TEXT",
            "downloaded_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE github_files ADD COLUMN {name} {definition}")


def parse_repository(value: str) -> tuple[str, str]:
    raw = value.strip()
    if raw.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        # Curated GitHub Pages source: its public library is backed by this repo.
        if parsed.hostname == "musetrainer.github.io" and parsed.path.rstrip("/") == "/library":
            return "musetrainer", "library"
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("Indica una URL de GitHub o una página de biblioteca compatible.")
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
def github_home(imported: int = 0):
    ensure_schema()
    with db() as conn:
        repositories = conn.execute("""
            SELECT r.*, COALESCE((SELECT COUNT(*) FROM github_files f WHERE f.repository_id=r.id),0) AS files,
                   (SELECT COUNT(*) FROM github_scans s WHERE s.repository_id=r.id) AS scans
            FROM github_repositories r ORDER BY COALESCE(r.last_scanned_at,r.added_at) DESC
        """).fetchall()
        recent_files = conn.execute("""
            SELECT f.*,r.full_name FROM github_files f JOIN github_repositories r ON r.id=f.repository_id
            WHERE f.extension <> '.txt'
            ORDER BY f.last_seen_at DESC LIMIT 40
        """).fetchall()
    body = """<h1>Fuentes GitHub</h1>
    <p><a href='/github/exploration'>Exploración persistente y presupuesto de búsqueda</a></p>
    <p>Registra repositorios públicos y examina su árbol de archivos para localizar partituras y formatos musicales.
    Puedes descargar candidatos para su análisis local. La procedencia GitHub se conserva y no se incorporan automáticamente a OSAP.</p>
    <section class="panel"><h2>Buscar repositorios con archivos musicales</h2>
    <form method="get" action="/github/search">
      <input name="q" type="search" style="min-width:min(32rem,100%)" placeholder="Opcional: compositor, colección, repertorio o tema..." />
      <select name="sort"><option value="updated">Actualizados recientemente</option><option value="stars">Más estrellas</option></select>
      <select name="license"><option value="">Cualquier licencia</option><option value="mit">MIT</option><option value="cc0-1.0">CC0 1.0</option><option value="cc-by-4.0">CC BY 4.0</option><option value="cc-by-sa-4.0">CC BY-SA 4.0</option><option value="gpl-3.0">GPL 3.0</option></select>
      <button>Buscar automáticamente</button>
    </form>
    <p class="muted">Sin escribir nada, explora repositorios por distintas familias de formatos y repertorios musicales. Después se inspecciona el árbol de cada repositorio para localizar los archivos concretos, sea cual sea su formato reconocido. El campo opcional sirve para afinar la búsqueda.</p></section>
    <section class="panel"><h2>Fuentes seleccionadas</h2>
    <p>MuseTrainer Library publica partituras MusicXML comprimidas (.mxl). Se añade como fuente seleccionada aunque no alcance el mínimo de 100 archivos de la exploración automática.</p>
    <form method="post" action="/github/add">
      <input name="repository" type="hidden" value="https://musetrainer.github.io/library">
      <button>Añadir MuseTrainer Library</button>
    </form>
    <p class="muted">Después pulsa «Leer repositorio» para inventariar los archivos de la carpeta scores. La lectura no descarga todavía las partituras.</p></section>
    <section class="panel"><h2>Añadir otro repositorio conocido</h2>
    <form method="post" action="/github/add">
      <input name="repository" type="text" required placeholder="OpenScore/Lieder o https://github.com/OpenScore/Lieder">
      <button>Añadir repositorio</button>
    </form>
    <p class="muted">Puedes registrar directamente cualquier repositorio público de GitHub. No pegues tokens ni credenciales en este formulario.</p></section>
    <section class="panel"><h2>Repositorios registrados</h2>"""
    if imported > 0:
        body += "<p class='panel'>Se han incorporado "+str(min(imported,10000))+" repositorios al catálogo. Pulsa «Leer repositorio» para obtener el inventario de archivos; todavía no se ha descargado ningún fichero.</p>"
    if repositories:
        body += "<table><tr><th>Repositorio</th><th>Licencia declarada</th><th>Archivos musicales</th><th>Última lectura</th><th>Acción</th></tr>"
        for repo in repositories:
            license_text = repo["license_spdx"] or repo["license_name"] or "No declarada"
            body += (
                "<tr><td><a href='" + esc(repo["html_url"]) + "'>" + esc(repo["full_name"]) + "</a>"
                "<div class='muted'>" + esc(repo["description"]) + "</div></td>"
                "<td>" + esc(license_text) + "</td><td>" + str(repo["files"]) + "</td>"
                "<td>" + esc(repo["last_scanned_at"] or "Sin explorar") + "</td>"
                "<td><form method='post' action='/github/" + str(repo["id"]) + "/scan'><button>Leer repositorio</button></form>"
                "<a href='/github/" + str(repo["id"]) + "'>Ver archivos</a></td></tr>"
            )
        body += "</table>"
    else:
        body += "<p>Aún no hay repositorios. Añade uno para empezar.</p>"
    body += "</section><section class='panel'><h2>Últimos archivos localizados</h2>"
    if recent_files:
        body += "<table><tr><th>Repositorio</th><th>Ruta</th><th>Formato</th><th>Tamaño</th></tr>"
        for item in recent_files:
            body += "<tr><td>" + esc(item["full_name"]) + "</td><td><a href='" + esc(item["html_url"]) + "'>" + esc(item["path"]) + "</a></td><td>" + esc(item["extension"]) + "</td><td>" + esc(item["size_bytes"] or "—") + "</td></tr>"
        body += "</table>"
    else:
        body += "<p class='muted'>Todavía no se han inventariado archivos.</p>"
    body += "</section><p><a href='/'>Volver al inicio</a> · <a href='/items'>Catálogo general</a></p>"
    return HTMLResponse(_page("Fuentes GitHub", body))


@router.get("/github/search", response_class=HTMLResponse)
async def github_search(q: str = "", sort: str = "updated", license: str = ""):
    """Queue repository discovery and inspection without holding the browser request."""
    ensure_schema()
    query = q.strip()[:160]
    if sort not in {"updated", "stars"}:
        sort = "updated"
    allowed_licenses = {"mit", "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "gpl-3.0"}
    if license not in allowed_licenses:
        license = ""

    from app import github_exploration as exploration
    exploration.ensure_schema()

    # Reuse the persistent exploration worker: discovery and tree inspection
    # happen in the background, while the browser immediately receives its
    # progress page. A custom search is stored as another resumable query.
    terms = [query] if query else list(exploration.QUERIES)
    if license:
        terms = [term + " license:" + license for term in terms]

    with exploration.db() as conn:
        for term in terms:
            conn.execute(
                """INSERT INTO github_discovery_queries(query,page,total_count,status,last_error,updated_at)
                   VALUES(?,1,NULL,'pending',NULL,?)
                   ON CONFLICT(query) DO UPDATE SET page=1,total_count=NULL,
                     status='pending',last_error=NULL,updated_at=excluded.updated_at""",
                (term, now()),
            )
        state = conn.execute(
            "SELECT job_status,enabled FROM github_exploration_state WHERE id=1"
        ).fetchone()
        if not state or state["job_status"] not in ("queued", "running") or not state["enabled"]:
            conn.execute(
                """UPDATE github_exploration_state
                   SET enabled=1,job_status='queued',requested_minutes=60,
                       progress_discoveries=0,progress_inspections=0,
                       last_message=? WHERE id=1""",
                ("Búsqueda automática en cola: " + (query or "todas las familias musicales") + ".",),
            )

    exploration.start_exploration_worker()
    return RedirectResponse("/github/exploration", status_code=303)


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
                    extension=excluded.extension,size_bytes=excluded.size_bytes,
                    download_status=CASE WHEN github_files.sha<>excluded.sha THEN 'not_downloaded' ELSE github_files.download_status END,
                    local_path=CASE WHEN github_files.sha<>excluded.sha THEN NULL ELSE github_files.local_path END,
                    sha256=CASE WHEN github_files.sha<>excluded.sha THEN NULL ELSE github_files.sha256 END,
                    downloaded_bytes=CASE WHEN github_files.sha<>excluded.sha THEN NULL ELSE github_files.downloaded_bytes END,
                    download_error=CASE WHEN github_files.sha<>excluded.sha THEN NULL ELSE github_files.download_error END,
                    sha=excluded.sha,blob_url=excluded.blob_url,html_url=excluded.html_url,last_seen_at=excluded.last_seen_at
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


@router.post("/github/files/{file_id}/download")
async def github_download_file(file_id: int):
    ensure_schema()
    with db() as conn:
        row = conn.execute("""
            SELECT f.*,r.full_name,r.default_branch
            FROM github_files f JOIN github_repositories r ON r.id=f.repository_id
            WHERE f.id=?
        """, (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Archivo GitHub no encontrado")
    if row["size_bytes"] is not None and int(row["size_bytes"]) > MAX_DOWNLOAD_BYTES:
        with db() as conn:
            conn.execute("UPDATE github_files SET download_status='too_large',download_error=? WHERE id=?",
                         ("Supera el límite configurado", file_id))
        return RedirectResponse(f"/github/{row['repository_id']}", status_code=303)

    parts = row["path"].split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Ruta GitHub no válida")
    owner, repository = row["full_name"].split("/", 1)
    raw_url = ("https://raw.githubusercontent.com/" + quote(owner, safe="") + "/"
        + quote(repository, safe="") + "/" + quote(row["default_branch"], safe="") + "/"
        + quote(row["path"], safe="/"))
    destination = DOWNLOAD_DIR / "github" / str(row["repository_id"])
    for part in parts:
        destination = destination / part
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    total = 0
    digest = hashlib.sha256()
    try:
        with db() as conn:
            conn.execute("UPDATE github_files SET download_status='downloading',download_error=NULL WHERE id=?", (file_id,))
        async with httpx.AsyncClient(timeout=90, headers=headers(), follow_redirects=True) as client:
            async with client.stream("GET", raw_url) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("El servidor anuncia un fichero mayor que el límite")
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes(128 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Descarga detenida al superar el límite de tamaño")
                        digest.update(chunk)
                        output.write(chunk)
        os.replace(temporary, destination)
        with db() as conn:
            downloaded_at = now()
            conn.execute("""UPDATE github_files SET download_status='downloaded',local_path=?,
                download_url=?,sha256=?,downloaded_bytes=?,downloaded_at=?,download_error=NULL WHERE id=?""",
                (str(destination), raw_url, digest.hexdigest(), total, downloaded_at, file_id))
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        with db() as conn:
            conn.execute("""UPDATE github_files SET download_status='error',download_error=?,
                downloaded_bytes=? WHERE id=?""", (str(exc)[:1000], total, file_id))
    return RedirectResponse(f"/github/{row['repository_id']}", status_code=303)


DOWNLOAD_CONCURRENCY = max(1, min(16, int(os.getenv("AI_IMPORTACION_DOWNLOAD_CONCURRENCY", "12"))))


async def _run_repository_downloads(repository_id: int, file_ids: list[int]) -> None:
    """Download with bounded concurrency; each file remains independently resumable."""
    with db() as conn:
        conn.execute("""UPDATE github_download_jobs SET status='running',started_at=?,updated_at=?,
            total=?,completed=0,failed=0,current_path='' WHERE repository_id=?""",
            (now(), now(), len(file_ids), repository_id))

    next_index = 0
    index_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal next_index
        while True:
            async with index_lock:
                if next_index >= len(file_ids):
                    return
                file_id = file_ids[next_index]
                next_index += 1

            with db() as conn:
                item = conn.execute(
                    "SELECT path FROM github_files WHERE id=? AND repository_id=?",
                    (file_id, repository_id),
                ).fetchone()
                if not item:
                    continue
                conn.execute(
                    "UPDATE github_download_jobs SET current_path=?,updated_at=? WHERE repository_id=?",
                    (item["path"], now(), repository_id),
                )

            try:
                await github_download_file(file_id)
            except Exception as exc:
                with db() as conn:
                    conn.execute(
                        "UPDATE github_files SET download_status='error',download_error=? WHERE id=?",
                        (str(exc)[:1000], file_id),
                    )

            with db() as conn:
                status_row = conn.execute(
                    "SELECT download_status FROM github_files WHERE id=?", (file_id,)
                ).fetchone()
                failed = int(bool(status_row) and status_row["download_status"] != "downloaded")
                conn.execute("""UPDATE github_download_jobs SET completed=completed+1,
                    failed=failed+?,updated_at=? WHERE repository_id=?""",
                    (failed, now(), repository_id))

    try:
        await asyncio.gather(*(worker() for _ in range(min(DOWNLOAD_CONCURRENCY, len(file_ids)))))
    except Exception:
        with db() as conn:
            conn.execute(
                "UPDATE github_download_jobs SET status='interrupted',updated_at=? WHERE repository_id=?",
                (now(), repository_id),
            )
        raise
    with db() as conn:
        conn.execute("""UPDATE github_download_jobs SET status='completed',current_path='',
            updated_at=? WHERE repository_id=?""", (now(), repository_id))


@router.post("/github/{repository_id}/download-all")
async def github_download_all(repository_id: int):
    ensure_schema()
    with db() as conn:
        repo = conn.execute("SELECT id FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        active = conn.execute(
            "SELECT status FROM github_download_jobs WHERE repository_id=?", (repository_id,)
        ).fetchone()
        if active and active["status"] == "running" and repository_id in _DOWNLOAD_TASKS:
            return RedirectResponse(f"/github/{repository_id}", status_code=303)
        file_ids = [row["id"] for row in conn.execute("""
            SELECT id FROM github_files WHERE repository_id=?
              AND extension <> '.txt'
              AND COALESCE(download_status,'not_downloaded')<>'downloaded'
              AND COALESCE(download_status,'not_downloaded')<>'too_large'
            ORDER BY path
        """, (repository_id,)).fetchall()]
        conn.execute("""INSERT INTO github_download_jobs
            (repository_id,status,total,completed,failed,current_path,started_at,updated_at)
            VALUES(?,'queued',?,0,0,'',?,?)
            ON CONFLICT(repository_id) DO UPDATE SET status='queued',total=excluded.total,
              completed=0,failed=0,current_path='',started_at=excluded.started_at,
              updated_at=excluded.updated_at""",
            (repository_id, len(file_ids), now(), now()))
    if file_ids:
        task = asyncio.create_task(_run_repository_downloads(repository_id, file_ids))
        _DOWNLOAD_TASKS[repository_id] = task
        task.add_done_callback(lambda finished, rid=repository_id: _DOWNLOAD_TASKS.pop(rid, None))
    else:
        with db() as conn:
            conn.execute("UPDATE github_download_jobs SET status='completed' WHERE repository_id=?",
                         (repository_id,))
    return RedirectResponse(f"/github/{repository_id}?download_all=started", status_code=303)


@router.get("/github/{repository_id}/download-status")
def github_download_status(repository_id: int):
    ensure_schema()
    with db() as conn:
        row = conn.execute("SELECT * FROM github_download_jobs WHERE repository_id=?",
                           (repository_id,)).fetchone()
        if not row:
            return JSONResponse({"status":"idle","total":0,"completed":0,"failed":0,"current_path":""})
        if row["status"] in {"queued","running"} and repository_id not in _DOWNLOAD_TASKS:
            conn.execute("UPDATE github_download_jobs SET status='interrupted',updated_at=? WHERE repository_id=?",
                         (now(), repository_id))
            row = conn.execute("SELECT * FROM github_download_jobs WHERE repository_id=?",
                               (repository_id,)).fetchone()
        return JSONResponse({key: row[key] for key in
            ("status","total","completed","failed","current_path","updated_at")})


@router.post("/github/{repository_id}/analyze")
def github_analyze_repository(repository_id: int):
    """Analyze downloaded GitHub MusicXML/MIDI files with the shared engine."""
    ensure_schema()
    from app.analysis_engine import analyze_local_music_file
    with db() as conn:
        repo = conn.execute("SELECT id FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        files = conn.execute("""
            SELECT id,path,local_path,sha256 FROM github_files
            WHERE repository_id=? AND download_status='downloaded' AND local_path IS NOT NULL
              AND extension IN ('.musicxml','.mxl','.xml','.mid','.midi','.kar')
            ORDER BY path
        """, (repository_id,)).fetchall()
    for item in files:
        previous = None
        with db() as conn:
            previous = conn.execute(
                "SELECT status,sha256,analyzer_version FROM github_file_analyses WHERE github_file_id=?",
                (item["id"],),
            ).fetchone()
        if (previous and previous["sha256"] and previous["sha256"] == item["sha256"]
                and previous["analyzer_version"] == ANALYZER_VERSION
                and previous["status"] in {"ok","not_musicxml"}):
            continue
        result = analyze_local_music_file(item["local_path"])
        with db() as conn:
            conn.execute("""
                INSERT INTO github_file_analyses
                  (github_file_id,analysis_type,status,analysis_json,error_text,analyzed_at,sha256,analyzer_version)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(github_file_id) DO UPDATE SET
                  analysis_type=excluded.analysis_type,status=excluded.status,
                  analysis_json=excluded.analysis_json,error_text=excluded.error_text,
                  analyzed_at=excluded.analyzed_at,sha256=excluded.sha256,
                  analyzer_version=excluded.analyzer_version
            """, (
                item["id"], result["analysis_type"], result["status"],
                __import__("json").dumps(result["analysis"], ensure_ascii=False),
                result["error_text"], now(), item["sha256"], ANALYZER_VERSION,
            ))
    return RedirectResponse(f"/github/{repository_id}?analysis=done", status_code=303)


@router.post("/github/{repository_id}/extract")
def github_extract_repository(repository_id: int):
    """Extract text locally from downloaded PDFs, images and notation files."""
    ensure_schema()
    from app.content_extraction import extract_local_content
    with db() as conn:
        repo = conn.execute("SELECT id FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        files = conn.execute("""
            SELECT id,path,local_path,sha256 FROM github_files
            WHERE repository_id=? AND download_status='downloaded' AND local_path IS NOT NULL
              AND extension IN ('.pdf','.txt','.ocr','.abc','.ly','.mei','.mscx','.mscz',
                                '.capx','.png','.jpg','.jpeg','.webp','.tif','.tiff','.bmp','.jp2')
            ORDER BY path
        """, (repository_id,)).fetchall()
    for item in files:
        with db() as conn:
            previous = conn.execute(
                "SELECT status,sha256 FROM github_content_extractions WHERE github_file_id=?",
                (item["id"],),
            ).fetchone()
        if previous and previous["sha256"] and previous["sha256"] == item["sha256"] and previous["status"] not in {"error","ocr_error","ocr_unavailable"}:
            continue
        result = extract_local_content(item["local_path"])
        with db() as conn:
            conn.execute("""
                INSERT INTO github_content_extractions
                  (github_file_id,status,page_count,extracted_characters,extracted_text,error_text,analyzed_at,sha256)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(github_file_id) DO UPDATE SET status=excluded.status,
                  page_count=excluded.page_count,extracted_characters=excluded.extracted_characters,
                  extracted_text=excluded.extracted_text,error_text=excluded.error_text,
                  analyzed_at=excluded.analyzed_at,sha256=excluded.sha256
            """, (item["id"], result["status"], result["page_count"],
                  result["extracted_characters"], result["text"][:200000],
                  result["error_text"], now(), item["sha256"]))
    return RedirectResponse(f"/github/{repository_id}?extraction=done", status_code=303)


@router.get("/github/{repository_id}/export-json")
def github_export_resources_json(repository_id: int):
    """Export one JSON document and its single source file per downloaded resource."""
    ensure_schema()
    with db() as conn:
        repo_row = conn.execute(
            "SELECT * FROM github_repositories WHERE id=?", (repository_id,)
        ).fetchone()
        if not repo_row:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        rows = conn.execute("""
            SELECT f.*,
                   a.analysis_type, a.status AS analysis_status,
                   a.analysis_json, a.error_text AS analysis_error,
                   a.analyzed_at, a.sha256 AS analysis_sha256, a.analyzer_version,
                   x.status AS extraction_status, x.page_count,
                   x.extracted_characters, x.extracted_text,
                   x.error_text AS extraction_error, x.analyzed_at AS extracted_at,
                   x.sha256 AS extraction_sha256,
                   ai.metadata_json AS ai_metadata_json, ai.sha256 AS ai_sha256,
                   ai.model AS ai_model, ai.analyzed_at AS ai_analyzed_at
            FROM github_files f
            LEFT JOIN github_file_analyses a ON a.github_file_id=f.id
            LEFT JOIN github_content_extractions x ON x.github_file_id=f.id
            LEFT JOIN github_ai_metadata_enrichments ai ON ai.github_file_id=f.id
            WHERE f.repository_id=? AND f.download_status='downloaded'
              AND f.local_path IS NOT NULL
            ORDER BY f.path
        """, (repository_id,)).fetchall()

    if not rows:
        raise HTTPException(status_code=409, detail="Este repositorio todavía no tiene ficheros descargados.")

    fd, archive_path = tempfile.mkstemp(prefix="ai-importacion-resources-", suffix=".zip")
    os.close(fd)
    exported = 0
    missing = []
    try:
        repository_readme = None
        readme_loaded = False
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for row in rows:
                local_path = Path(row["local_path"])
                if not local_path.is_file():
                    missing.append(row["path"])
                    continue
                resource_key = stable_resource_id(repo_row["full_name"], row["path"])
                extension = Path(row["path"]).suffix.lower()
                package_file = "files/" + resource_key + extension
                analysis = {
                    "type": row["analysis_type"],
                    "status": row["analysis_status"] or "not_analyzed",
                    "metadata": {},
                    "error": row["analysis_error"],
                    "analyzed_at": row["analyzed_at"],
                }
                analysis_is_current = (bool(row["analysis_status"])
                    and row["analysis_sha256"] == row["sha256"]
                    and row["analyzer_version"] == ANALYZER_VERSION)
                analyzable_extensions = {".musicxml", ".mxl", ".xml", ".mid", ".midi", ".kar"}
                if Path(row["path"]).suffix.lower() in analyzable_extensions and not analysis_is_current:
                    from app.analysis_engine import analyze_local_music_file
                    result = analyze_local_music_file(local_path)
                    analysis = {
                        "type": result["analysis_type"],
                        "status": result["status"],
                        "metadata": result["analysis"],
                        "error": result["error_text"],
                        "analyzed_at": now(),
                    }
                    with db() as conn:
                        conn.execute("""
                            INSERT INTO github_file_analyses
                              (github_file_id,analysis_type,status,analysis_json,error_text,analyzed_at,sha256,analyzer_version)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(github_file_id) DO UPDATE SET
                              analysis_type=excluded.analysis_type,status=excluded.status,
                              analysis_json=excluded.analysis_json,error_text=excluded.error_text,
                              analyzed_at=excluded.analyzed_at,sha256=excluded.sha256,
                              analyzer_version=excluded.analyzer_version
                        """, (row["id"], analysis["type"], analysis["status"],
                              json.dumps(analysis["metadata"], ensure_ascii=False),
                              analysis["error"], analysis["analyzed_at"], row["sha256"], ANALYZER_VERSION))
                elif analysis_is_current and row["analysis_json"]:
                    try:
                        analysis["metadata"] = json.loads(row["analysis_json"])
                    except (TypeError, ValueError):
                        analysis["error"] = "El resultado de análisis guardado no contiene JSON válido."
                elif row["analysis_status"]:
                    analysis["status"] = "stale"
                extraction_current = bool(row["extraction_status"]) and row["extraction_sha256"] == row["sha256"]
                extraction = {
                    "status": (row["extraction_status"] if extraction_current
                               else ("stale" if row["extraction_status"] else "not_extracted")),
                    "page_count": row["page_count"] or 0,
                    "character_count": row["extracted_characters"] or 0,
                    "text": row["extracted_text"] if extraction_current else "",
                    "error": row["extraction_error"],
                    "extracted_at": row["extracted_at"],
                }
                metadata = analysis.get("metadata") or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                has_title = bool(metadata.get("work_title") or metadata.get("title"))
                has_creators = bool(metadata.get("creators"))
                cached_ai = None
                if row["ai_metadata_json"] and row["ai_sha256"] == row["sha256"]:
                    try:
                        cached_ai = json.loads(row["ai_metadata_json"])
                    except (TypeError, ValueError):
                        cached_ai = None
                if not has_title or not has_creators:
                    if cached_ai is None:
                        from app.metadata_enrichment import enrich_music_metadata
                        ai_context = dict(metadata)
                        if extraction["text"]:
                            ai_context["source_text_excerpt"] = extraction["text"][:3500]
                        if not readme_loaded:
                            repository_readme = fetch_repository_readme(repo_row["full_name"])
                            readme_loaded = True
                            if repository_readme:
                                with db() as conn:
                                    conn.execute(
                                        "UPDATE github_repositories SET readme_text=? WHERE id=?",
                                        (repository_readme, repository_id),
                                    )
                        if repository_readme:
                            ai_context["repository_readme_excerpt"] = repository_readme[:5000]
                        cached_ai = enrich_music_metadata(
                            filename=row["name"],
                            repository=dict(repo_row),
                            metadata=ai_context,
                        )
                        if cached_ai.get("status") == "ok":
                            with db() as conn:
                                conn.execute("""
                                    INSERT INTO github_ai_metadata_enrichments
                                      (github_file_id,sha256,metadata_json,model,analyzed_at,error_text)
                                    VALUES(?,?,?,?,?,NULL)
                                    ON CONFLICT(github_file_id) DO UPDATE SET
                                      sha256=excluded.sha256,metadata_json=excluded.metadata_json,
                                      model=excluded.model,analyzed_at=excluded.analyzed_at,error_text=NULL
                                """, (row["id"], row["sha256"] or "",
                                      json.dumps(cached_ai, ensure_ascii=False),
                                      cached_ai.get("model") or "", now()))
                    if cached_ai and cached_ai.get("status") == "ok":
                        if not has_title and cached_ai.get("title"):
                            metadata["title"] = cached_ai["title"]
                            metadata["title_source"] = "ai:filename_and_metadata"
                        existing_creators = list(metadata.get("creators") or [])
                        for creator in cached_ai.get("creators") or []:
                            if not any(
                                str(old.get("type", "")).casefold() == str(creator.get("type", "")).casefold()
                                and str(old.get("name", "")).casefold() == str(creator.get("name", "")).casefold()
                                for old in existing_creators if isinstance(old, dict)
                            ):
                                existing_creators.append({
                                    "name": creator["name"], "type": creator["type"],
                                })
                        metadata["creators"] = existing_creators
                        if cached_ai.get("creator_candidates"):
                            metadata["creator_candidates"] = (
                                list(metadata.get("creator_candidates") or [])
                                + cached_ai["creator_candidates"]
                            )
                        if cached_ai.get("title_candidates"):
                            metadata["title_candidates"] = (
                                list(metadata.get("title_candidates") or [])
                                + cached_ai["title_candidates"]
                            )
                        metadata["ai_enrichment"] = {
                            "status": "ok", "model": cached_ai.get("model"),
                            "analyzed_at": row["ai_analyzed_at"] or now(),
                        }
                    elif cached_ai and cached_ai.get("status") == "not_configured":
                        metadata["ai_enrichment"] = {"status": "not_configured"}
                    elif cached_ai and cached_ai.get("status") == "error":
                        metadata["ai_enrichment"] = {
                            "status": "error", "error": cached_ai.get("error"),
                        }
                analysis["metadata"] = metadata
                document = build_github_resource_document(
                    dict(repo_row),
                    {
                        "id": row["id"], "path": row["path"], "name": row["name"],
                        "extension": row["extension"], "size_bytes": row["size_bytes"],
                        "downloaded_bytes": row["downloaded_bytes"], "sha256": row["sha256"],
                        "git_sha": row["sha"], "html_url": row["html_url"],
                        "download_url": row["download_url"] or (
                            "https://raw.githubusercontent.com/" + quote(repo_row["full_name"].split("/", 1)[0], safe="") + "/"
                            + quote(repo_row["full_name"].split("/", 1)[1], safe="") + "/"
                            + quote(repo_row["default_branch"], safe="") + "/"
                            + quote(row["path"], safe="/")
                        ),
                        "downloaded_at": row["downloaded_at"],
                        "first_seen_at": row["first_seen_at"],
                        "last_seen_at": row["last_seen_at"],
                    },
                    analysis,
                    extraction,
                    package_file=package_file,
                )
                archive.writestr("resources/" + resource_key + ".json", encode_resource_json(document))
                archive.write(local_path, package_file)
                exported += 1
            archive.writestr("manifest.json", encode_resource_json({
                "schema": "osap-resource-import-package/1.0",
                "provider": "github",
                "repository": repo_row["full_name"],
                "resource_count": exported,
                "omitted_missing_files": missing,
            }))
        if not exported:
            os.unlink(archive_path)
            raise HTTPException(status_code=409, detail="No se encontró ningún fichero descargado en disco.")
        filename = str(repo_row["full_name"]).replace("/", "-") + "-resources-json.zip"
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(os.unlink, archive_path),
        )
    except Exception:
        if os.path.exists(archive_path):
            os.unlink(archive_path)
        raise


@router.get("/github/{repository_id}", response_class=HTMLResponse)
def github_repository(repository_id: int, q: str = ""):
    ensure_schema()
    with db() as conn:
        repo = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        files = conn.execute("""
            SELECT f.*,a.status AS analysis_status,a.sha256 AS analysis_sha256,a.analysis_json,a.error_text AS analysis_error,x.status AS extraction_status,x.sha256 AS extraction_sha256,x.extracted_characters,x.extracted_text,x.error_text AS extraction_error
            FROM github_files f LEFT JOIN github_file_analyses a ON a.github_file_id=f.id LEFT JOIN github_content_extractions x ON x.github_file_id=f.id
            WHERE f.repository_id=? AND f.extension <> '.txt'
              AND (?='' OR f.path LIKE ?)
            ORDER BY f.path LIMIT 500
        """, (repository_id, q, "%" + q + "%")).fetchall()
        scan = conn.execute("SELECT * FROM github_scans WHERE repository_id=? ORDER BY id DESC LIMIT 1", (repository_id,)).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM github_files WHERE repository_id=? AND extension <> '.txt'", (repository_id,)).fetchone()[0]
        download_counts = conn.execute("""
            SELECT
              SUM(CASE WHEN download_status='downloaded' THEN 1 ELSE 0 END) AS downloaded,
              SUM(CASE WHEN COALESCE(download_status,'not_downloaded') NOT IN ('downloaded','too_large') THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN download_status='too_large' THEN 1 ELSE 0 END) AS too_large
            FROM github_files WHERE repository_id=? AND extension <> '.txt'
        """, (repository_id,)).fetchone()
        downloaded_count = int(download_counts["downloaded"] or 0)
        pending_count = int(download_counts["pending"] or 0)
        too_large_count = int(download_counts["too_large"] or 0)
    body = "<h1>" + esc(repo["full_name"]) + "</h1><p>" + esc(repo["description"]) + "</p>"
    body += "<p><a href='" + esc(repo["html_url"]) + "'>Abrir en GitHub</a> · Licencia declarada: <b>" + esc(repo["license_spdx"] or repo["license_name"] or "No declarada") + "</b></p>"
    body += "<section class='panel' style='border:2px solid #174f89;background:#fff'>"
    body += "<h2 style='margin-top:0'>Descarga del repositorio</h2>"
    body += "<p><b>" + str(total) + " archivos inventariados</b> · " + str(downloaded_count) + " descargados · " + str(pending_count) + " pendientes"
    if too_large_count:
        body += " · " + str(too_large_count) + " superan el límite de descarga"
    body += "</p>"
    if pending_count:
        body += "<form method='post' action='/github/" + str(repository_id) + "/download-all'><button style='font-size:1.1rem;padding:.9rem 1.3rem'>Descargar los " + str(pending_count) + " archivos pendientes</button></form>"
    elif downloaded_count:
        body += "<p><b>Todos los archivos que se pueden descargar ya están descargados.</b></p>"
    else:
        body += "<p>No hay archivos pendientes de descarga.</p>"
    body += "<section id='download-progress' data-status-url='/github/" + str(repository_id) + "/download-status'><b>Progreso:</b> <span id='download-state'>consultando…</span><p id='download-detail'></p><progress id='download-bar' max='100' value='0' style='width:100%'></progress></section></section>"
    if downloaded_count:
        body += "<section class='panel'><h2>Preparar datos de los recursos</h2>"
        body += "<p>Genera un paquete ZIP con un JSON por cada fichero descargado y el propio fichero. Incluye los metadatos y análisis disponibles; todavía no incorpora nada a OSAP.</p>"
        body += "<a class='button' href='/github/" + str(repository_id) + "/export-json'>Generar paquete JSON (" + str(downloaded_count) + " recursos)</a></section>"
        body += "<details class='panel'><summary>Herramientas posteriores a la descarga</summary>"
        body += "<p>Estas operaciones solo tienen sentido después de descargar los archivos.</p>"
        body += "<form method='post' action='/github/" + str(repository_id) + "/analyze'><button>Analizar archivos musicales descargados</button></form>"
        body += "<form method='post' action='/github/" + str(repository_id) + "/extract'><button>Extraer texto, PDF y OCR de archivos descargados</button></form>"
        body += "<p class='muted'>El análisis y la extracción se guardan por separado del catálogo de Internet Archive.</p></details>"
    body += "<details class='panel'><summary>Opciones avanzadas del repositorio</summary>"
    body += "<form method='post' action='/github/" + str(repository_id) + "/scan'><button>Volver a leer el repositorio</button></form>"
    body += "<p><a href='/github/" + str(repository_id) + "/acquisition'>Documentación, referencias y plan de adquisición</a></p></details>"
    if scan:
        body += "<p class='muted'>Última lectura: " + esc(scan["completed_at"] or scan["started_at"]) + " · " + str(scan["candidate_count"]) + " archivos candidatos · " + ("árbol truncado: la lectura puede ser parcial" if scan["truncated"] else "árbol completo") + "</p>"
        if scan["error_text"]:
            body += "<p class='muted'>" + esc(scan["error_text"]) + "</p>"
    body += "<h2>Archivos localizados (" + str(total) + ")</h2><form method='get'><input name='q' type='text' value='" + esc(q) + "' placeholder='Filtrar por ruta'><button>Filtrar</button></form>"
    if files:
        folder_counts = {}
        for item in files:
            folder = str(Path(item["path"]).parent).replace("\\", "/")
            if folder == ".":
                folder = "(raíz del repositorio)"
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
        body += "<section class='panel'><h3>Carpetas con archivos localizados</h3><ul class='folder-index'>"
        for folder, count in sorted(folder_counts.items(), key=lambda pair: pair[0].casefold()):
            body += "<li><a href='#folder-" + esc(quote(folder, safe='')) + "'><code>" + esc(folder) + "</code></a> · " + str(count) + " archivos</li>"
        body += "</ul></section>"
        body += "<table><tr><th>Archivo</th><th>Formato</th><th>Tamaño</th><th>SHA Git</th><th>Descarga local</th><th>Análisis musical</th><th>Extracción PDF/OCR</th></tr>"
        previous_folder = None
        for item in files:
            folder = str(Path(item["path"]).parent).replace("\\", "/")
            if folder == ".":
                folder = "(raíz del repositorio)"
            if folder != previous_folder:
                body += "<tr class='folder-heading' id='folder-" + esc(quote(folder, safe='')) + "'><th colspan='7'>Carpeta: <code>" + esc(folder) + "</code> · " + str(folder_counts[folder]) + " archivos</th></tr>"
                previous_folder = folder
            status = item["download_status"] or "not_downloaded"
            if status == "downloaded":
                action = "Descargado · " + str(item["downloaded_bytes"] or 0) + " bytes"
            elif status == "too_large":
                action = "Supera el límite configurado"
            else:
                label = "Reintentar" if status == "error" else "Descargar"
                action = "<form method='post' action='/github/files/" + str(item["id"]) + "/download'><button>" + label + "</button></form>"
            if item["download_error"]:
                action += "<div class='muted'>" + esc(item["download_error"]) + "</div>"
            analysis_status = item["analysis_status"] or "pendiente"
            if item["analysis_sha256"] and item["analysis_sha256"] != item["sha256"]:
                analysis_status = "stale"
            analysis_label = {"ok": "Partitura MusicXML/MIDI válida", "not_musicxml": "XML genérico (no es partitura)", "error": "Error", "stale": "Desactualizado"}.get(analysis_status, "Pendiente")
            if item["analysis_status"] == "ok" and item["analysis_json"]:
                try:
                    metadata = __import__("json").loads(item["analysis_json"])
                    if metadata.get("title"):
                        analysis_label += " · " + str(metadata["title"])
                    creators = metadata.get("creators") or []
                    creator_names = [str(value.get("name", "")).strip() for value in creators if value.get("name")]
                    if creator_names:
                        analysis_label += " · " + ", ".join(creator_names[:3])
                    if metadata.get("part_count") is not None:
                        analysis_label += " · " + str(metadata["part_count"]) + " partes"
                except (TypeError, ValueError):
                    pass
            if item["analysis_error"]:
                analysis_label += " · " + item["analysis_error"][:120]
            extraction_status = item["extraction_status"] or "pendiente"
            if item["extraction_sha256"] and item["extraction_sha256"] != item["sha256"]:
                extraction_status = "desactualizado"
            extraction_label = extraction_status
            if item["extraction_status"]:
                extraction_label += " · " + str(item["extracted_characters"] or 0) + " caracteres"
            if item["extraction_error"]:
                extraction_label += " · " + item["extraction_error"][:100]
            extraction_preview = ""
            if item["extracted_text"]:
                extraction_preview = "<details><summary>Ver texto extraído</summary><pre style='white-space:pre-wrap;max-height:22rem;overflow:auto'>" + esc(item["extracted_text"][:12000]) + "</pre></details>"
            body += "<tr><td><a href='" + esc(item["html_url"]) + "'>" + esc(item["path"]) + "</a></td><td>" + esc(item["extension"]) + "</td><td>" + esc(item["size_bytes"] or "—") + "</td><td><code>" + esc(item["sha"][:12]) + "</code></td><td>" + action + "</td><td>" + esc(analysis_label) + "</td><td>" + esc(extraction_label) + extraction_preview + "</td></tr>"
        body += "</table>"
    else:
        body += "<p>No hay archivos inventariados que coincidan. Ejecuta una lectura del repositorio.</p>"
    body += "<p><a href='/github'>Volver a fuentes GitHub</a></p>"
    body += """<script>
(async function(){
 const panel=document.getElementById('download-progress'); if(!panel)return;
 const url=panel.dataset.statusUrl;
 async function refresh(){
  try{
   const response=await fetch(url,{cache:'no-store'}); if(!response.ok)return;
   const d=await response.json();
   const state=document.getElementById('download-state');
   const detail=document.getElementById('download-detail');
   const bar=document.getElementById('download-bar');
   state.textContent=d.status==='running'?'En curso':d.status==='queued'?'En cola':d.status==='completed'?'Finalizada':d.status==='interrupted'?'Interrumpida; pulsa de nuevo para continuar':'Sin iniciar';
   detail.textContent=d.completed+' de '+d.total+' archivos procesados · '+d.failed+' errores'+(d.current_path?' · Actual: '+d.current_path:'');
   bar.value=d.total?Math.round(d.completed*100/d.total):0;
   if(d.status==='running'||d.status==='queued')setTimeout(refresh,2000);
  }catch(e){setTimeout(refresh,5000);}
 }
 refresh();
})();
</script>"""
    return HTMLResponse(_page("Repositorio " + repo["full_name"], body))


def _page(title: str, body: str) -> str:
    style = """<style>
    body{font:16px system-ui,sans-serif;color:#20242a;background:#f4f6f8;margin:0}
    header{background:#15283f;color:white;padding:1rem max(1rem,calc((100% - 1100px)/2))}
    header a{color:white;margin-right:1.2rem;text-decoration:none}
    main{max-width:1100px;margin:1.5rem auto;padding:0 1rem}
    .panel{background:white;border:1px solid #dce2e8;border-radius:10px;padding:1rem;margin:1rem 0}
    input,button{font:inherit;padding:.65rem;border:1px solid #aab6c4;border-radius:6px}
    button{background:#174f89;color:white;border:0;cursor:pointer}.button{display:inline-block;background:#174f89;color:white;padding:.75rem 1rem;border-radius:6px;text-decoration:none}
    table{border-collapse:collapse;width:100%;background:white}td,th{text-align:left;padding:.65rem;border-bottom:1px solid #e1e6eb;vertical-align:top}
    th{background:#edf1f5}.muted{color:#5d6977;font-size:.9rem}a{color:#175ca8}.folder-index{columns:2;column-gap:2rem}.folder-index li{break-inside:avoid;margin:.25rem 0}.folder-heading th{background:#dfe8f1;padding-top:1rem}
    </style>"""
    return ("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + esc(title) + "</title>" + style +
            "<header><b><a href='/'>AI Importación</a></b><a href='/items'>Catálogo local</a><a href='/intake'>Captación integrada</a><a href='/github'>Fuentes GitHub</a></header>"
            "<main>" + body + "<p class='muted'>La presencia de un fichero público no acredita derechos de redistribución. Los archivos descargados se conservan localmente y no se escriben en OSAP.</p></main></html>")
