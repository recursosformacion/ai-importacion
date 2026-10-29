from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.file_catalog import (
    download_file, esc, inventory_item_files,
)
from app.db import connect

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
router = APIRouter()


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    return connect(DB_PATH)


def priority(file_name: str, archive_format: str, size_bytes: int | None) -> tuple[int, str]:
    """Rank likely score files first; keep unknown extensions visible but do not auto-fetch them."""
    name = file_name.casefold()
    suffix = Path(name).suffix
    fmt = (archive_format or "").casefold()
    if any(x in name for x in ("thumb", "thumbnail", "tile", "scandata", "djvu.xml", "page_numbers",
                               "hocr", "chocr", "ocr_search", "ocr_page", "abbyy", "log", "torrent")):
        return 0, "Auxiliar de Internet Archive"
    if suffix in {".musicxml", ".mxl", ".sib", ".musx", ".mscz", ".mscx", ".capx", ".mus",
                  ".ove", ".enc", ".nwc", ".nwctxt", ".ly", ".mei", ".abc", ".gp", ".gp3",
                  ".gp4", ".gp5", ".gpx", ".gp7", ".mid", ".midi"}:
        return 100, "Partitura editable o datos musicales"
    if suffix == ".pdf" or "pdf" in fmt or "musical score" in fmt:
        if any(x in name for x in ("text", "ocr", "fulltext", "additional", "search")):
            return 20, "PDF de texto auxiliar"
        return 90, "Partitura o documento PDF"
    if suffix in {".djvu", ".ps"} or "djvu" in fmt or "postscript" in fmt:
        return 65, "Documento/partitura digitalizada"
    if suffix in {".zip", ".7z", ".tar", ".gz"}:
        return 55, "Archivo comprimido que puede contener partituras"
    if suffix in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".jp2"} or "image" in fmt:
        if size_bytes and size_bytes <= 20 * 1024 * 1024:
            return 35, "Imagen que podría ser una página musical"
        return 15, "Imagen o conjunto de páginas; prioridad baja"
    if suffix in {".txt", ".ocr", ".html"} and any(x in name for x in ("fulltext", "contents", "toc", "djvu", "ocr")):
        return 25, "Texto OCR o índice para análisis"
    # Deliberately do not auto-download unknown extensions, but inventory them.
    return 0, "Formato auxiliar o aún no clasificado"


async def inventory_one(identifier: str) -> int:
    return await inventory_item_files(identifier)


def layout(title: str, body: str) -> HTMLResponse:
    style = """<style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20242a}
    .panel{border:1px solid #dce2e8;border-radius:10px;padding:1rem;margin:1rem 0}button,select{font:inherit;padding:.65rem}
    button{background:#174f89;color:white;border:0;border-radius:6px;cursor:pointer}table{border-collapse:collapse;width:100%}
    td,th{text-align:left;padding:.6rem;border-bottom:1px solid #e1e6eb;vertical-align:top}th{background:#edf1f5}
    .muted{color:#5d6977;font-size:.9rem}</style>"""
    safe_title = esc(title)
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>"
        + safe_title + "</title>" + style + "<p><a href='/'>← Inicio</a> · <a href='/items'>Catálogo local</a></p><h1>"
        + safe_title + "</h1>" + body + "</html>")


@router.get("/workflow", response_class=HTMLResponse)
def workflow_home():
    with db() as conn:
        counts = {
            "candidates": conn.execute("SELECT COUNT(*) FROM global_discovery_seen WHERE score>0").fetchone()[0],
            "inventoried": conn.execute("SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok'").fetchone()[0],
            "files": conn.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0],
            "downloaded": conn.execute("SELECT COUNT(*) FROM archive_files WHERE download_status='downloaded'").fetchone()[0],
            "pending": conn.execute("""SELECT COUNT(*) FROM global_discovery_seen s LEFT JOIN archive_item_inventories i
                ON i.identifier=s.identifier WHERE s.score>0 AND (i.identifier IS NULL OR i.status='error')""").fetchone()[0],
        }
        rows = conn.execute("""SELECT f.identifier,a.title,f.file_name,f.archive_format,f.size_bytes,
                f.download_status,f.local_path
            FROM archive_files f JOIN archive_items a ON a.identifier=f.identifier
            JOIN global_discovery_seen s ON s.identifier=f.identifier
            WHERE s.score>0 AND f.download_status='downloaded'
            ORDER BY f.id DESC LIMIT 30""").fetchall()
    body = "<p>Este flujo combina inventario y descarga selectiva. Para cada candidato nuevo consulta todos los metadatos de sus ficheros y, en el mismo proceso, descarga los archivos con mayor prioridad. No escribe en OSAP.</p><div class='panel'><table>"
    body += "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k,v in [
        ("Candidatos musicales",counts["candidates"]),("Inventarios correctos",counts["inventoried"]),
        ("Archivos registrados",counts["files"]),("Descargas locales",counts["downloaded"]),
        ("Candidatos pendientes de inventario",counts["pending"])]) + "</table></div>"
    body += """<section class='panel'><h2>Procesar candidatos</h2>
      <form method='post' action='/workflow/run'>
      <label>Elementos por ejecución <select name='batch_size'><option value='5'>5</option><option selected value='10'>10</option><option value='20'>20</option></select></label>
      <label>Máximo de descargas por elemento <select name='downloads_per_item'><option value='1'>1</option><option selected value='2'>2</option><option value='3'>3</option></select></label>
      <button>Inventariar y descargar prioritarios</button></form>
      <p class='muted'>Prioridad: formatos editables (MusicXML, MXL, Sibelius y otros), después PDF, documentos escaneados y archivos comprimidos. Se omiten automáticamente miniaturas, OCR auxiliar y formatos desconocidos; estos últimos permanecen inventariados y no se descartan.</p></section>"""
    if rows:
        body += "<section class='panel'><h2>Descargas recientes</h2><table><tr><th>Elemento</th><th>Archivo</th><th>Formato</th><th>Tamaño</th><th>Ruta local</th></tr>"
        for r in rows:
            size = f"{r['size_bytes']:,} bytes" if r["size_bytes"] is not None else "—"
            body += f"<tr><td>{esc(r['title'] or r['identifier'])}<br><span class='muted'>{esc(r['identifier'])}</span></td><td>{esc(r['file_name'])}</td><td>{esc(r['archive_format'] or 'No indicado')}</td><td>{esc(size)}</td><td>{esc(r['local_path'] or '—')}</td></tr>"
        body += "</table></section>"
    return layout("Inventario y descarga integrados", body)


@router.post("/workflow/run", response_class=HTMLResponse)
async def run_workflow(batch_size: int = Form(10), downloads_per_item: int = Form(2)):
    batch_size = max(1, min(int(batch_size), 20))
    downloads_per_item = max(1, min(int(downloads_per_item), 3))
    with db() as conn:
        targets = conn.execute("""SELECT a.identifier,a.title,
              CASE WHEN i.identifier IS NULL OR i.status='error' THEN 0 ELSE 1 END AS inventoried
            FROM global_discovery_seen s JOIN archive_items a ON a.identifier=s.identifier
            LEFT JOIN archive_item_inventories i ON i.identifier=s.identifier
            WHERE s.score>0 AND (
              i.identifier IS NULL OR i.status='error' OR EXISTS (
                SELECT 1 FROM archive_files f WHERE f.identifier=a.identifier AND f.relevant=1
                  AND f.download_status IN ('not_downloaded','error')
              )
            )
            ORDER BY s.score DESC,s.seen_at DESC LIMIT ?""", (batch_size,)).fetchall()
    inventoried_now = 0
    files_seen = 0
    downloads_started = 0
    failures = []
    for target in targets:
        identifier = target["identifier"]
        try:
            if not target["inventoried"]:
                files_seen += await inventory_one(identifier)
                inventoried_now += 1
            with db() as conn:
                files = conn.execute("""SELECT id,file_name,archive_format,size_bytes,download_status
                    FROM archive_files WHERE identifier=? AND relevant=1
                    AND download_status IN ('not_downloaded','error')
                    ORDER BY CASE
                      WHEN lower(file_name) LIKE '%.musicxml' OR lower(file_name) LIKE '%.mxl'
                        OR lower(file_name) LIKE '%.sib' OR lower(file_name) LIKE '%.mscz'
                        OR lower(file_name) LIKE '%.mscx' OR lower(file_name) LIKE '%.capx'
                        OR lower(file_name) LIKE '%.musx' OR lower(file_name) LIKE '%.ly'
                        OR lower(file_name) LIKE '%.mei' OR lower(file_name) LIKE '%.abc'
                        OR lower(file_name) LIKE '%.mid' OR lower(file_name) LIKE '%.midi' THEN 0
                      WHEN lower(file_name) LIKE '%.pdf' THEN 1
                      WHEN lower(file_name) LIKE '%.djvu' OR lower(file_name) LIKE '%.ps' THEN 2
                      WHEN lower(file_name) LIKE '%.zip' OR lower(file_name) LIKE '%.7z' THEN 3
                      ELSE 4 END,
                    CASE WHEN size_bytes IS NULL THEN 1 ELSE 0 END,size_bytes ASC LIMIT ?""",
                    (identifier, downloads_per_item)).fetchall()
            for f in files:
                rank, why = priority(f["file_name"], f["archive_format"] or "", f["size_bytes"])
                if rank <= 0:
                    continue
                if f["size_bytes"] is not None and f["size_bytes"] > int(os.getenv("AI_IMPORTACION_MAX_DOWNLOAD_BYTES", str(100*1024*1024))):
                    continue
                await download_file(int(f["id"]))
                downloads_started += 1
        except Exception as exc:
            failures.append(f"{identifier}: {str(exc)[:250]}")
            with db() as conn:
                conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                    VALUES(?,?,0,'error',?) ON CONFLICT(identifier) DO UPDATE SET
                    checked_at=excluded.checked_at,status='error',error_text=excluded.error_text""",
                    (identifier, now(), str(exc)[:1000]))
    with db() as conn:
        downloaded_total = conn.execute("SELECT COUNT(*) FROM archive_files WHERE download_status='downloaded'").fetchone()[0]
        inventories_total = conn.execute("SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok'").fetchone()[0]
    body = f"<section class='panel'><p>Elementos procesados: <b>{len(targets)}</b>.</p><p>Nuevos inventarios: <b>{inventoried_now}</b>.</p><p>Archivos incorporados/actualizados al inventario: <b>{files_seen}</b>.</p><p>Descargas intentadas: <b>{downloads_started}</b>.</p><p>Inventarios correctos acumulados: <b>{inventories_total}</b>.</p><p>Descargas correctas acumuladas: <b>{downloaded_total}</b>.</p>"
    if failures:
        body += "<h3>Incidencias</h3><ul>" + "".join("<li>" + esc(x) + "</li>" for x in failures) + "</ul>"
    body += "<p>Las descargas respetan el límite por fichero y guardan SHA-256. Los archivos auxiliares se conservan en el inventario, pero no se descargan automáticamente. <a href='/workflow'>Volver al flujo</a></p></section>"
    return layout("Resultado del procesamiento", body)
