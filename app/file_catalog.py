from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
DOWNLOAD_DIR = Path(os.getenv("AI_IMPORTACION_DOWNLOAD_DIR", "downloads"))
MAX_DOWNLOAD_BYTES = int(os.getenv("AI_IMPORTACION_MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024)))
METADATA_ENDPOINT = "https://archive.org/metadata/"
USER_AGENT = "OSAP-AI-Importacion/0.2 (+https://openmusicrepository.com)"
SUPPORTED_EXTENSIONS = {
    ".pdf", ".xml", ".musicxml", ".mxl", ".mid", ".midi", ".zip", ".capx",
    ".mus", ".sib", ".mscz", ".ly", ".ps", ".djvu", ".jpg", ".jpeg", ".png", ".tif", ".tiff"
}
router = APIRouter()


def db():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value):
    import html
    return html.escape("" if value is None else str(value))


def safe_filename(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._() -]", "_", base).strip(" .")
    return base[:180] or "archive-file"


def valid_identifier(identifier: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", identifier))


def relevant_file(name: str, fmt: str) -> bool:
    suffix = Path(name.lower()).suffix
    format_text = fmt.lower()
    return suffix in SUPPORTED_EXTENSIONS or any(word in format_text for word in (
        "pdf", "musicxml", "midi", "musical score", "scorch", "capella", "sibelius",
        "finale", "compressed score", "djvu", "image", "postscript", "lilypond"
    ))


@router.post("/items/{identifier}/inspect-files")
async def inspect_files(identifier: str):
    if not valid_identifier(identifier):
        raise HTTPException(status_code=422, detail="Identificador no válido")
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Elemento no encontrado en el catálogo")
    try:
        async with httpx.AsyncClient(timeout=45, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            response = await client.get(METADATA_ENDPOINT + quote(identifier, safe=""))
            response.raise_for_status()
            payload = response.json()
        files = payload.get("files", [])
        if not isinstance(files, list):
            raise ValueError("Respuesta de inventario no válida")
        stamp = now()
        with db() as conn:
            for item in files:
                name = str(item.get("name", "")).strip()
                if not name or name in {".", ".."} or any(part == ".." for part in name.replace("\\\\", "/").split("/")):
                    continue
                try:
                    size = int(item["size"]) if item.get("size") not in (None, "") else None
                except (ValueError, TypeError):
                    size = None
                fmt = str(item.get("format") or "")
                source_url = "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(name, safe="/")
                conn.execute("""INSERT INTO archive_files
                  (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,md5,sha1,relevant,inventory_seen_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(identifier,file_name) DO UPDATE SET file_name_safe=excluded.file_name_safe,
                  source_url=excluded.source_url,size_bytes=excluded.size_bytes,archive_format=excluded.archive_format,
                  md5=excluded.md5,sha1=excluded.sha1,relevant=excluded.relevant,inventory_seen_at=excluded.inventory_seen_at""",
                  (identifier, name, safe_filename(name), source_url, size, fmt, item.get("md5"), item.get("sha1"),
                   int(relevant_file(name, fmt)), stamp))
        return RedirectResponse(url="/files/" + quote(identifier, safe=""), status_code=303)
    except HTTPException:
        raise
    except Exception as exc:
        return HTMLResponse("<h1>Error al consultar Internet Archive</h1><p>" + esc(str(exc)) + "</p><p><a href='/files/" + esc(identifier) + "'>Volver</a></p>", status_code=502)


@router.get("/files/{identifier}", response_class=HTMLResponse)
def file_catalog(identifier: str):
    if not valid_identifier(identifier):
        raise HTTPException(status_code=422, detail="Identificador no válido")
    with db() as conn:
        item = conn.execute("SELECT title FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
        files = conn.execute("SELECT * FROM archive_files WHERE identifier=? ORDER BY relevant DESC, size_bytes, file_name", (identifier,)).fetchall()
    if not item:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")
    body = "<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ficheros de origen</title><style>body{font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#20242a}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #ddd;vertical-align:top}button{padding:.5rem .7rem;cursor:pointer}small{color:#596575}code{overflow-wrap:anywhere}</style>"
    body += "<p><a href='/items/" + esc(identifier) + "'>← Volver a la ficha</a></p><h1>Ficheros: " + esc(item["title"] or identifier) + "</h1>"
    body += "<p>Inventario de Internet Archive. Los ficheros descargados quedan en almacenamiento local; esta función no escribe en OSAP ni Storage.</p>"
    body += "<form method='post' action='/items/" + esc(identifier) + "/inspect-files'><button>Consultar / actualizar inventario</button></form>"
    body += "<p><small>Límite de descarga: " + str(MAX_DOWNLOAD_BYTES // (1024*1024)) + " MiB por fichero. Los formatos relevantes se pueden descargar individualmente.</small></p>"
    if not files:
        body += "<p>Inventario aún no consultado. Pulsa el botón para recuperar nombres, formatos, tamaños y checksums de origen.</p>"
    else:
        body += "<table><tr><th>Fichero / formato</th><th>Tamaño</th><th>Checksums</th><th>Estado</th><th>Acción</th></tr>"
        for f in files:
            size = f"{f['size_bytes']:,} bytes" if f["size_bytes"] is not None else "—"
            checksums = "MD5: " + esc(f["md5"] or "—") + "<br>SHA-1 origen: " + esc(f["sha1"] or "—") + "<br>SHA-256 local: " + esc(f["sha256"] or "—")
            action = ""
            if f["relevant"] and f["download_status"] != "downloaded":
                action = f"<form method='post' action='/files/download/{f['id']}'><button>Descargar</button></form>"
            elif f["download_status"] == "downloaded":
                action = "<span>Guardado localmente</span>"
            body += f"<tr><td>{esc(f['file_name'])}<br><small>{esc(f['archive_format'] or 'Formato no indicado')}{' · candidato musical' if f['relevant'] else ''}</small></td><td>{esc(size)}</td><td><code>{checksums}</code></td><td>{esc(f['download_status'])}<br>{esc(f['download_error'] or '')}</td><td>{action}</td></tr>"
        body += "</table>"
    body += "</html>"
    return HTMLResponse(body)


@router.post("/files/download/{file_id}")
async def download_file(file_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM archive_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Fichero no encontrado")
    if not row["relevant"]:
        raise HTTPException(status_code=422, detail="El formato no está clasificado como candidato musical")
    if row["size_bytes"] is not None and row["size_bytes"] > MAX_DOWNLOAD_BYTES:
        with db() as conn:
            conn.execute("UPDATE archive_files SET download_status='too_large',download_error=? WHERE id=?",
                         ("Supera el límite configurado", file_id))
        return RedirectResponse(url="/files/" + quote(row["identifier"], safe=""), status_code=303)
    folder = DOWNLOAD_DIR / row["identifier"]
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / safe_filename(row["file_name"])
    temporary = destination.with_name(destination.name + ".part")
    total = 0
    digest = hashlib.sha256()
    try:
        with db() as conn:
            conn.execute("UPDATE archive_files SET download_status='downloading',download_error=NULL WHERE id=?", (file_id,))
        async with httpx.AsyncClient(timeout=90, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            async with client.stream("GET", row["source_url"]) as response:
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
            conn.execute("""UPDATE archive_files SET download_status='downloaded',local_path=?,sha256=?,
                downloaded_bytes=?,download_error=NULL WHERE id=?""",
                (str(destination), digest.hexdigest(), total, file_id))
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        with db() as conn:
            conn.execute("UPDATE archive_files SET download_status='error',download_error=?,downloaded_bytes=? WHERE id=?",
                         (str(exc)[:1000], total, file_id))
    return RedirectResponse(url="/files/" + quote(row["identifier"], safe=""), status_code=303)
