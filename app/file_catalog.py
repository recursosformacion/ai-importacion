from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
DOWNLOAD_DIR = Path(os.getenv("AI_IMPORTACION_DOWNLOAD_DIR", "downloads"))
MAX_DOWNLOAD_BYTES = int(os.getenv("AI_IMPORTACION_MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024)))
METADATA_ENDPOINT = "https://archive.org/metadata/"
USER_AGENT = "OSAP-AI-Importacion/0.2 (+https://openmusicrepository.com)"
# Broad inventory of notation, score-editor, archive, scan and supporting-text formats.
# Unknown formats are still recorded; this set only affects prioritization/download eligibility.
SUPPORTED_EXTENSIONS = {
    ".pdf", ".djvu", ".ps",
    ".xml", ".musicxml", ".mxl", ".mei", ".abc", ".ly",
    ".mid", ".midi", ".kar",
    ".capx", ".mus", ".musx", ".sib", ".mscz", ".mscx", ".ove", ".enc",
    ".nwc", ".nwctxt", ".gp", ".gp3", ".gp4", ".gp5", ".gpx", ".gp7",
    ".mgu", ".sgu",
    ".zip", ".7z", ".tar", ".gz",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".jp2",
    ".txt", ".ocr", ".html"
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
    return (suffix in SUPPORTED_EXTENSIONS and (suffix != ".txt" or any(token in name.lower() for token in ("djvu", "ocr", "fulltext", "full_text", "contents", "toc")))) or any(word in format_text for word in (
        "pdf", "musicxml", "midi", "musical score", "scorch", "capella", "sibelius",
        "finale", "compressed score", "djvu", "image", "postscript", "lilypond", "djvu text", "ocr text"
    ))


def download_status_label(status: str | None, error: str | None = None) -> str:
    labels = {
        "not_downloaded": "Pendiente de descarga",
        "downloading": "Descarga en curso",
        "downloaded": "Guardado localmente",
        "error": "Error de descarga",
        "too_large": "No descargado: supera el límite configurado",
    }
    label = labels.get(status or "not_downloaded", "Estado: " + str(status))
    if status == "error" and error:
        return label + " · se puede reintentar"
    return label


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
            conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                VALUES(?,?,?,'ok',NULL) ON CONFLICT(identifier) DO UPDATE SET
                checked_at=excluded.checked_at,file_count=excluded.file_count,status='ok',error_text=NULL""",
                (identifier, stamp, conn.execute(
                    "SELECT COUNT(*) FROM archive_files WHERE identifier=?", (identifier,)
                ).fetchone()[0]))
        return RedirectResponse(url="/files/" + quote(identifier, safe=""), status_code=303)
    except HTTPException:
        raise
    except Exception as exc:
        body = "<nav class='workflow' aria-label='Pasos de captación'><b>Captación musical · paso 3 de 5</b><p>Todo permanece en esta aplicación local; no se crean ni exportan obras OSAP.</p></nav>"
        body += "<section><h1>No se pudo actualizar el inventario</h1><p>Internet Archive no ha respondido correctamente. No se ha confirmado un inventario nuevo; puedes volver a intentarlo.</p><p>Detalle técnico: " + esc(str(exc)) + "</p><p><a href='/files/" + esc(identifier) + "'>← Volver al inventario y reintentar</a></p></section>"
        return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><title>Error de inventario</title><body>" + body + "</body></html>", status_code=502)


@router.get("/files/{identifier}", response_class=HTMLResponse)
def file_catalog(identifier: str):
    if not valid_identifier(identifier):
        raise HTTPException(status_code=422, detail="Identificador no válido")
    with db() as conn:
        item = conn.execute("SELECT title FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
        files = conn.execute("SELECT * FROM archive_files WHERE identifier=? ORDER BY relevant DESC, size_bytes, file_name", (identifier,)).fetchall()
    if not item:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")
    body = "<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ficheros de origen</title><style>body{font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#20242a;background:#f4f6f8}table{width:100%;border-collapse:collapse;background:#fff}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #ddd;vertical-align:top}button{padding:.5rem .7rem;cursor:pointer;background:#174f89;color:white;border:0;border-radius:6px;font:inherit}small{color:#596575}code{overflow-wrap:anywhere}a{color:#175ca8}.workflow{background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:1rem;margin:1rem 0}.workflow-steps{display:flex;gap:.5rem;flex-wrap:wrap;margin:.8rem 0}.workflow-steps a{display:block;padding:.65rem .85rem;border:1px solid #cbd5e1;border-radius:8px;text-decoration:none;background:#f8fafc;color:#174f89}.workflow-steps a span{display:inline-grid;place-items:center;border-radius:50%;background:#e5ebf2;min-width:1.5rem;height:1.5rem;margin-right:.25rem}.workflow-steps a.active{background:#174f89;color:#fff;border-color:#174f89;font-weight:700}.workflow-steps a.active span{background:#fff;color:#174f89}.workflow-help{margin:.4rem 0;color:#5d6977}.section-nav{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin:1rem 0;padding-top:.7rem;border-top:1px solid #e1e6eb}</style>"
    ident = quote(identifier, safe="")
    body += "<nav class='workflow' aria-label='Pasos de captación'><b>Captación musical · paso 3 de 5</b><div class='workflow-steps'><a href='/'> <span>1</span> Buscar</a><a href='/items/" + esc(ident) + "'><span>2</span> Elegir elemento</a><a class='active' href='/files/" + esc(ident) + "'><span>3</span> Archivos</a><a href='/items/" + esc(ident) + "/content-analysis#analisis'><span>4</span> Analizar</a><a href='/items/" + esc(ident) + "/content-analysis?step=5#obras'><span>5</span> Revisar obras</a></div><p class='workflow-help'>Sigue los pasos de izquierda a derecha. Puedes volver a cualquiera sin perder lo guardado. Todo permanece en esta aplicación local; no se exporta a OSAP.</p></nav>"
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
            status = f["download_status"] or "not_downloaded"
            if f["relevant"] and status in {"not_downloaded", "error"}:
                button_label = "Reintentar descarga" if status == "error" else "Descargar"
                action = f"<form method='post' action='/files/download/{f['id']}'><button>{button_label}</button></form>"
            elif status == "downloaded":
                action = "<span>Guardado localmente</span>"
            elif status == "too_large":
                action = "<span>No disponible por el límite de tamaño</span>"
            body += f"<tr><td>{esc(f['file_name'])}<br><small>{esc(f['archive_format'] or 'Formato no indicado')}{' · texto de apoyo para análisis' if Path(f['file_name'].lower()).suffix == '.txt' else (' · candidato musical' if f['relevant'] else '')}</small></td><td>{esc(size)}</td><td><code>{checksums}</code></td><td>{esc(download_status_label(status, f['download_error']))}<br>{esc(f['download_error'] or '')}</td><td>{action}</td></tr>"
        body += "</table>"
    body += "<div class='section-nav'><a href='/items/" + esc(identifier) + "'>← Paso anterior: elemento</a><a href='/items/" + esc(identifier) + "/content-analysis'>Siguiente: analizar contenido →</a></div>"
    body += "</html>"
    return HTMLResponse(body)


@router.post("/files/download/{file_id}")
async def download_file(file_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM archive_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Fichero no encontrado")
    with db() as conn:
        inventory = conn.execute(
            "SELECT status FROM archive_item_inventories WHERE identifier=?",
            (row["identifier"],),
        ).fetchone()
    if not inventory or inventory["status"] != "ok":
        raise HTTPException(
            status_code=409,
            detail="No se puede descargar: el elemento no tiene un inventario correcto registrado.",
        )
    if not row["relevant"]:
        raise HTTPException(status_code=422, detail="El formato no está clasificado como candidato musical")
    if row["size_bytes"] is not None and row["size_bytes"] > MAX_DOWNLOAD_BYTES:
        with db() as conn:
            conn.execute("UPDATE archive_files SET download_status='too_large',download_error=? WHERE id=?",
                         ("Supera el límite configurado", file_id))
        return RedirectResponse(url="/files/" + quote(row["identifier"], safe=""), status_code=303)
    folder = DOWNLOAD_DIR / row["identifier"]
    folder.mkdir(parents=True, exist_ok=True)
    local_name = safe_filename(row["file_name"])
    with db() as conn:
        collisions = conn.execute(
            "SELECT COUNT(*) FROM archive_files WHERE identifier=? AND file_name_safe=? AND file_name<>?",
            (row["identifier"], row["file_name_safe"], row["file_name"]),
        ).fetchone()[0]
    if collisions:
        safe_path = Path(local_name)
        suffix = safe_path.suffix
        stem = safe_path.name[:-len(suffix)] if suffix else safe_path.name
        digest = hashlib.sha256(row["file_name"].encode("utf-8")).hexdigest()[:10]
        local_name = stem[:150] + "-" + digest + suffix
    destination = folder / local_name
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



# Integrated intake: inventory new candidates and download a small number of likely score files.
def _intake_priority(name: str, fmt: str, size: int | None) -> int:
    suffix = Path(name.lower()).suffix
    f = (fmt or "").casefold()
    # Prefer editable notation, then score PDFs; do not discard unfamiliar extensions
    # from inventory, but avoid automatically downloading unknown files.
    priority = {
        ".musicxml": 120, ".mxl": 118, ".xml": 105, ".mei": 100, ".abc": 98,
        ".sib": 96, ".mscz": 95, ".mscx": 94, ".musx": 92, ".mus": 90,
        ".capx": 90, ".ove": 88, ".ly": 88, ".nwc": 86, ".nwctxt": 85,
        ".pdf": 80, ".djvu": 60, ".ps": 58, ".zip": 50, ".7z": 45,
        ".mid": 30, ".midi": 30, ".kar": 28,
        ".jpg": 18, ".jpeg": 18, ".png": 18, ".tif": 18, ".tiff": 18, ".jp2": 10,
        ".txt": 8, ".ocr": 8, ".html": 5,
    }.get(suffix, 0)
    if any(token in f for token in ("musicxml", "compressed score", "sibelius", "capella", "finale", "lilypond")):
        priority += 20
    if any(token in name.casefold() for token in ("thumb", "thumbnail", "tile", "scandata", "djvu.xml", "hocr", "chocr", "ocr_search", "page_numbers", "torrent", "metadata")):
        priority -= 100
    if size is not None:
        if size <= 0:
            priority -= 100
        elif size > MAX_DOWNLOAD_BYTES:
            priority -= 80
        elif size < 1024:
            priority -= 15
    return priority


@router.get("/intake", response_class=HTMLResponse)
def intake_page():
    with db() as conn:
        totals = conn.execute("""SELECT
            (SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok') AS inventoried,
            (SELECT COUNT(*) FROM archive_files WHERE download_status='downloaded') AS downloaded,
            (SELECT COUNT(*) FROM global_discovery_seen WHERE score>0) AS candidates,
            (SELECT COUNT(*) FROM archive_files WHERE download_status='error') AS errors""").fetchone()
        recent = conn.execute("""SELECT f.identifier,a.title,f.file_name,f.archive_format,f.size_bytes,
            f.download_status,f.sha256,f.local_path
            FROM archive_files f JOIN archive_items a ON a.identifier=f.identifier
            WHERE f.download_status IN ('downloaded','error','too_large')
            ORDER BY f.id DESC LIMIT 30""").fetchall()
    body = "<nav class='workflow' aria-label='Pasos de captación'><b>Captación musical · paso 3 de 5</b><div class='workflow-steps'><a href='/'><span>1</span> Buscar</a><a href='/items'><span>2</span> Elegir elemento</a><a class='active' href='/intake'><span>3</span> Archivos</a><a href='/items'><span>4</span> Analizar</a><a href='/items'><span>5</span> Revisar obras</a></div><p class='workflow-help'>Esta vista permite inventariar y descargar archivos de candidatos por lotes. Para analizar uno en concreto, selecciónalo en el catálogo.</p></nav><p><a href='/'>← Inicio</a></p><h1>Captación integrada: inventariar y descargar</h1>"
    body += "<p>Este proceso combina ambas tareas: inventaría candidatos que aún no tienen inventario y descarga unos pocos archivos priorizados. No descarga en masa y no escribe en OSAP.</p>"
    body += "<div style='display:flex;gap:1rem;flex-wrap:wrap'>" + "".join(
        f"<div style='background:#f1f4f7;padding:1rem;border-radius:8px'><small>{label}</small><h2>{value or 0}</h2></div>"
        for label,value in [("Candidatos musicales",totals["candidates"]),("Elementos inventariados",totals["inventoried"]),("Archivos descargados",totals["downloaded"]),("Errores de descarga",totals["errors"])]
    ) + "</div>"
    body += "<section style='padding:1rem;margin:1rem 0;background:#fff;border:1px solid #ddd;border-radius:8px'><form method='post' action='/intake/run'>"
    body += "<label>Candidatos nuevos a inventariar <select name='items'><option>5</option><option selected>10</option><option>20</option></select></label> "
    body += "<label>Máximo de descargas en esta ejecución <select name='downloads'><option>1</option><option selected>3</option><option>5</option><option>10</option></select></label> "
    body += "<button style='padding:.7rem 1rem;cursor:pointer'>Inventariar y descargar</button></form>"
    body += "<p><small>Prioridad: formatos editables de notación, PDF y otros formatos de partitura. Se excluyen auxiliares obvios y archivos por encima del límite de tamaño configurado. Los formatos desconocidos permanecen registrados, pero no se descargan automáticamente hasta clasificarlos.</small></p></section>"
    body += "<h2>Actividad reciente</h2><table><tr><th>Elemento / archivo</th><th>Formato</th><th>Tamaño</th><th>Estado</th><th>SHA-256 local</th></tr>"
    for row in recent:
        size = f"{row['size_bytes']:,} bytes" if row["size_bytes"] is not None else "—"
        body += f"<tr><td><a href='/files/{quote(row['identifier'], safe='')}'>{esc(row['title'] or row['identifier'])}</a><br><small>{esc(row['file_name'])}</small></td><td>{esc(row['archive_format'] or Path(row['file_name']).suffix or 'Desconocido')}</td><td>{esc(size)}</td><td>{esc(download_status_label(row['download_status']))}</td><td><code>{esc(row['sha256'] or '—')}</code></td></tr>"
    body += "</table>"
    return HTMLResponse(body)


@router.post("/intake/run", response_class=HTMLResponse)
async def run_integrated_intake(items: int = Form(10), downloads: int = Form(3)):
    items = max(1, min(int(items), 20))
    downloads = max(0, min(int(downloads), 10))
    inventoried_now = 0
    inventory_errors: list[str] = []
    downloaded_now = 0
    download_errors: list[str] = []

    # First inventory only a small batch of candidates not yet inventoried successfully.
    with db() as conn:
        targets = conn.execute("""SELECT a.identifier,a.title FROM global_discovery_seen s
            JOIN archive_items a ON a.identifier=s.identifier
            LEFT JOIN archive_item_inventories i ON i.identifier=s.identifier
            WHERE s.score>0 AND (i.identifier IS NULL OR i.status='error')
            ORDER BY s.score DESC,s.seen_at DESC LIMIT ?""", (items,)).fetchall()

    for target in targets:
        identifier = target["identifier"]
        try:
            async with httpx.AsyncClient(timeout=45, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
                response = await client.get(METADATA_ENDPOINT + quote(identifier, safe=""))
                response.raise_for_status()
                payload = response.json()
            files = payload.get("files", [])
            if not isinstance(files, list):
                raise ValueError("Respuesta de inventario no válida")
            stamp = now()
            recorded = 0
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
                    url = "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(name, safe="/")
                    conn.execute("""INSERT INTO archive_files
                        (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,md5,sha1,relevant,inventory_seen_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(identifier,file_name) DO UPDATE SET file_name_safe=excluded.file_name_safe,
                        source_url=excluded.source_url,size_bytes=excluded.size_bytes,archive_format=excluded.archive_format,
                        md5=excluded.md5,sha1=excluded.sha1,relevant=excluded.relevant,inventory_seen_at=excluded.inventory_seen_at""",
                        (identifier,name,safe_filename(name),url,size,fmt,item.get("md5"),item.get("sha1"),int(relevant_file(name,fmt)),stamp))
                    recorded += 1
                conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                    VALUES(?,?,?,'ok',NULL) ON CONFLICT(identifier) DO UPDATE SET checked_at=excluded.checked_at,
                    file_count=excluded.file_count,status='ok',error_text=NULL""", (identifier,stamp,recorded))
            inventoried_now += 1
        except Exception as exc:
            inventory_errors.append(f"{identifier}: {str(exc)[:180]}")
            with db() as conn:
                conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                    VALUES(?,?,0,'error',?) ON CONFLICT(identifier) DO UPDATE SET checked_at=excluded.checked_at,
                    status='error',error_text=excluded.error_text""", (identifier,now(),str(exc)[:1000]))

    # Then select highest-priority eligible files already catalogued, including prior inventory batches.
    if downloads:
        with db() as conn:
            all_files = conn.execute("""SELECT f.* FROM archive_files f
                JOIN global_discovery_seen s ON s.identifier=f.identifier
                JOIN archive_item_inventories i ON i.identifier=f.identifier AND i.status='ok'
                WHERE s.score>0 AND f.relevant=1 AND f.download_status IN ('not_downloaded','error')
                ORDER BY s.score DESC,f.id ASC""").fetchall()
        ranked = sorted(all_files, key=lambda row: _intake_priority(row["file_name"], row["archive_format"], row["size_bytes"]), reverse=True)
        for row in ranked:
            if downloaded_now >= downloads:
                break
            priority = _intake_priority(row["file_name"], row["archive_format"], row["size_bytes"])
            if priority <= 0:
                continue
            if row["size_bytes"] is not None and row["size_bytes"] > MAX_DOWNLOAD_BYTES:
                with db() as conn:
                    conn.execute("UPDATE archive_files SET download_status='too_large',download_error=? WHERE id=?", ("Supera el límite configurado", row["id"]))
                continue
            await download_file(int(row["id"]))
            with db() as conn:
                result = conn.execute("SELECT download_status FROM archive_files WHERE id=?", (row["id"],)).fetchone()
            if result and result["download_status"] == "downloaded":
                downloaded_now += 1
            else:
                download_errors.append(f"{row['identifier']} / {row['file_name']}: descarga fallida")

    summary = f"<p>Inventarios nuevos correctos: <b>{inventoried_now}</b> de {len(targets)} candidatos consultados.</p><p>Descargas correctas en esta ejecución: <b>{downloaded_now}</b> de un máximo de {downloads}.</p>"
    if inventory_errors:
        summary += "<h3>Errores de inventario</h3><ul>" + "".join(f"<li>{esc(x)}</li>" for x in inventory_errors) + "</ul>"
    if download_errors:
        summary += "<h3>Errores de descarga</h3><ul>" + "".join(f"<li>{esc(x)}</li>" for x in download_errors) + "</ul>"
    summary += "<p>Los inventarios y descargas se guardan en SQLite y en la carpeta local de descargas. No se ha escrito en OSAP.</p><p><a href='/intake'>Ver captación integrada</a> · <a href='/'>Inicio</a></p>"
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Resultado de captación</title><body style='font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem'>" + summary + "</body></html>")
