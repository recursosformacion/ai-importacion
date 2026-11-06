from __future__ import annotations

import hashlib
import html
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import DOWNLOAD_DIR, MAX_DOWNLOAD_BYTES, connect

router = APIRouter()
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
          sha256 TEXT
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
        CREATE INDEX IF NOT EXISTS idx_github_files_extension ON github_files(extension);
        CREATE INDEX IF NOT EXISTS idx_github_files_repo ON github_files(repository_id,path);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(github_files)")}
        additions = {
            "download_status": "TEXT NOT NULL DEFAULT 'not_downloaded'",
            "local_path": "TEXT",
            "sha256": "TEXT",
            "downloaded_bytes": "INTEGER",
            "download_error": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE github_files ADD COLUMN {name} {definition}")


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
    <section class="panel"><h2>Añadir un repositorio conocido</h2>
    <form method="post" action="/github/add">
      <input name="repository" type="text" required placeholder="OpenScore/Lieder o https://github.com/OpenScore/Lieder">
      <button>Añadir repositorio</button>
    </form>
    <p class="muted">También puedes registrar directamente un repositorio público si ya conoces su dirección. No pegues tokens ni credenciales en este formulario.</p></section>
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
    """Discover repositories automatically, or narrow discovery with an optional topic."""
    ensure_schema()
    query = q.strip()
    if sort not in {"updated", "stars"}:
        sort = "updated"
    allowed_licenses = {"mit", "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "gpl-3.0"}
    if license not in allowed_licenses:
        license = ""

    # GitHub repository search does not index every file in every repository.
    # Run several independent discovery queries so the user need not know a
    # repository name or a particular musical extension. The subsequent tree
    # scan is what confirms the actual files and their extensions.
    discovery_queries = [query] if query else [
        "music", "sheet music", "musical scores", "musicxml",
        "midi", "lilypond", "abc notation",
    ]
    if license:
        discovery_queries = [term + " license:" + license for term in discovery_queries]

    merged = {}
    totals = []
    errors = []
    for term in discovery_queries:
        params = urlencode({"q": term + " fork:false", "sort": sort,
                            "order": "desc", "per_page": 30})
        try:
            payload = await github_json(f"{API}/search/repositories?{params}")
            totals.append(int(payload.get("total_count") or 0))
            for item in payload.get("items") or []:
                full_name = item.get("full_name")
                if full_name:
                    current = merged.get(full_name)
                    if current is None or (item.get("updated_at") or "") > (current.get("updated_at") or ""):
                        merged[full_name] = item
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(str(exc))
            if not query and len(errors) == len(discovery_queries):
                break

    discovered = list(merged.values())
    discovered.sort(
        key=lambda item: (
            int(item.get("stargazers_count") or 0) if sort == "stars"
            else str(item.get("updated_at") or "")
        ),
        reverse=True,
    )

    # Verify actual repository contents before presenting a useful candidate.
    results = []
    inspected = 0
    inspection_errors = []
    for item in discovered[:MAX_REPOSITORIES_TO_INSPECT]:
        try:
            musical_count, tree_truncated = await count_musical_pieces(item)
            inspected += 1
            if musical_count >= MIN_MUSICAL_PIECES:
                item["musical_file_count"] = musical_count
                item["tree_truncated"] = tree_truncated
                results.append(item)
        except (ValueError, httpx.HTTPError) as exc:
            inspection_errors.append(str(exc))
    total = sum(totals)
    body = "<h1>Repositorios candidatos</h1>"
    body += "<form method='get' action='/github/search'><input name='q' type='search' value='" + esc(query) + "' placeholder='Opcional: compositor, colección o tema...' />"
    body += "<select name='sort'><option value='updated'" + (" selected" if sort == "updated" else "") + ">Actualizados recientemente</option>"
    body += "<option value='stars'" + (" selected" if sort == "stars" else "") + ">Más estrellas</option></select>"
    body += "<select name='license'><option value=''>Cualquier licencia</option>"
    for value, label in (("mit","MIT"),("cc0-1.0","CC0 1.0"),("cc-by-4.0","CC BY 4.0"),("cc-by-sa-4.0","CC BY-SA 4.0"),("gpl-3.0","GPL 3.0")):
        body += "<option value='" + value + "'" + (" selected" if license == value else "") + ">" + label + "</option>"
    body += "</select><button>Buscar</button></form>"
    if query:
        body += "<p class='muted'>Búsqueda afinada por: " + esc(query) + ". "
    else:
        body += "<p class='muted'>Búsqueda automática por siete familias de términos musicales. "
    body += "GitHub ha devuelto " + str(total) + " coincidencias acumuladas; se muestran hasta 60 repositorios únicos."
    body += " Se han inspeccionado " + str(inspected) + " árboles; sólo se muestran repositorios con al menos 11 archivos musicales reconocibles.</p>"
    if errors:
        body += "<p class='muted'>Algunas consultas de búsqueda no pudieron completarse: " + esc("; ".join(errors)) + "</p>"
    if inspection_errors:
        body += "<p class='muted'>No se pudieron inspeccionar algunos repositorios: " + esc("; ".join(inspection_errors[:3])) + "</p>"
    if results:
        body += "<table><tr><th>Repositorio</th><th>Archivos musicales reconocidos</th><th>Lenguaje</th><th>Estrellas</th><th>Licencia declarada</th><th>Actualizado</th><th></th></tr>"
        for item in results:
            full_name = item.get("full_name") or ""
            license_data = item.get("license") or {}
            license_name = license_data.get("spdx_id") or license_data.get("name") or "No declarada"
            body += "<tr><td><a href='" + esc(item.get("html_url") or "") + "'><b>" + esc(full_name) + "</b></a><div class='muted'>" + esc(item.get("description") or "") + "</div></td>"
            body += "<td>" + str(item.get("musical_file_count", 0)) + ("+" if item.get("tree_truncated") else "") + "</td>"
            body += "<td>" + esc(item.get("language") or "—") + "</td><td>" + str(int(item.get("stargazers_count") or 0)) + "</td>"
            body += "<td>" + esc(license_name) + "</td><td>" + esc((item.get("updated_at") or "")[:10]) + "</td>"
            body += "<td><form method='post' action='/github/add'><input type='hidden' name='repository' value='" + esc(full_name) + "'><button>Añadir al catálogo</button></form></td></tr>"
        body += "</table>"
        body += "<p class='muted'>El recuento se basa en extensiones musicales explícitas del árbol GitHub. Archivos genéricos .xml/.txt, PDF, imágenes y ZIP no se cuentan como piezas confirmadas.</p>"
    else:
        body += "<p>No se han encontrado repositorios en estas consultas. Puedes probar una búsqueda afinada o añadir una URL conocida.</p>"
    body += "<p><a href='/github'>Volver a Fuentes GitHub</a></p>"
    return HTMLResponse(_page("Búsqueda GitHub", body))

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
            conn.execute("""UPDATE github_files SET download_status='downloaded',local_path=?,
                sha256=?,downloaded_bytes=?,download_error=NULL WHERE id=?""",
                (str(destination), digest.hexdigest(), total, file_id))
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        with db() as conn:
            conn.execute("""UPDATE github_files SET download_status='error',download_error=?,
                downloaded_bytes=? WHERE id=?""", (str(exc)[:1000], total, file_id))
    return RedirectResponse(f"/github/{row['repository_id']}", status_code=303)


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
                "SELECT status,sha256 FROM github_file_analyses WHERE github_file_id=?",
                (item["id"],),
            ).fetchone()
        if previous and previous["sha256"] and previous["sha256"] == item["sha256"] and previous["status"] in {"ok","not_musicxml"}:
            continue
        result = analyze_local_music_file(item["local_path"])
        with db() as conn:
            conn.execute("""
                INSERT INTO github_file_analyses
                  (github_file_id,analysis_type,status,analysis_json,error_text,analyzed_at,sha256)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(github_file_id) DO UPDATE SET
                  analysis_type=excluded.analysis_type,status=excluded.status,
                  analysis_json=excluded.analysis_json,error_text=excluded.error_text,
                  analyzed_at=excluded.analyzed_at,sha256=excluded.sha256
            """, (
                item["id"], result["analysis_type"], result["status"],
                __import__("json").dumps(result["analysis"], ensure_ascii=False),
                result["error_text"], now(), item["sha256"],
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
    body = "<h1>" + esc(repo["full_name"]) + "</h1><p>" + esc(repo["description"]) + "</p>"
    body += "<p><a href='" + esc(repo["html_url"]) + "'>Abrir en GitHub</a> · Licencia declarada: <b>" + esc(repo["license_spdx"] or repo["license_name"] or "No declarada") + "</b></p>"
    body += "<form method='post' action='/github/" + str(repository_id) + "/scan'><button>Volver a leer el repositorio</button></form>"
    body += "<form method='post' action='/github/" + str(repository_id) + "/analyze'><button>Analizar archivos musicales descargados</button></form>"
    body += "<form method='post' action='/github/" + str(repository_id) + "/extract'><button>Extraer texto, PDF y OCR de archivos descargados</button></form>"
    body += "<p class='muted'>Extracción local de PDF, imágenes y formatos de notación. Los resultados permanecen separados de Internet Archive.</p>"
    body += "<p class='muted'>El análisis utiliza el mismo motor estructural que Internet Archive para MusicXML, MXL, XML y MIDI. Los resultados GitHub se guardan por separado, sin mezclar catálogos ni procedencias.</p>"
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
    th{background:#edf1f5}.muted{color:#5d6977;font-size:.9rem}a{color:#175ca8}.folder-index{columns:2;column-gap:2rem}.folder-index li{break-inside:avoid;margin:.25rem 0}.folder-heading th{background:#dfe8f1;padding-top:1rem}
    </style>"""
    return ("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + esc(title) + "</title>" + style +
            "<header><b><a href='/'>AI Importación</a></b><a href='/items'>Catálogo local</a><a href='/intake'>Captación integrada</a><a href='/github'>Fuentes GitHub</a></header>"
            "<main>" + body + "<p class='muted'>La presencia de un fichero público no acredita derechos de redistribución. Los archivos descargados se conservan localmente y no se escriben en OSAP.</p></main></html>")
