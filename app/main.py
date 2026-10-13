from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
ARCHIVE_ENDPOINT = "https://archive.org/advancedsearch.php"
FIELDS = ["identifier", "title", "creator", "date", "mediatype", "collection", "rights", "description", "subject", "language", "publisher"]
USER_AGENT = "OSAP-AI-Importacion/0.1 (+https://openmusicrepository.com; metadata research)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT NOT NULL, archive_query TEXT NOT NULL,
          rows_requested INTEGER NOT NULL, total_found INTEGER NOT NULL DEFAULT 0,
          result_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'running',
          error_text TEXT, started_at TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS archive_items(
          identifier TEXT PRIMARY KEY, title TEXT, creator_json TEXT NOT NULL DEFAULT 'null',
          item_date TEXT, mediatype TEXT, collection_json TEXT NOT NULL DEFAULT 'null',
          rights_json TEXT NOT NULL DEFAULT 'null', description_json TEXT NOT NULL DEFAULT 'null',
          source_url TEXT NOT NULL, metadata_json TEXT NOT NULL, first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending',
          rights_status TEXT NOT NULL DEFAULT 'unreviewed');
        CREATE TABLE IF NOT EXISTS search_run_items(
          search_run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          result_rank INTEGER NOT NULL, PRIMARY KEY(search_run_id,identifier));
        CREATE TABLE IF NOT EXISTS archive_files(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          file_name TEXT NOT NULL, file_name_safe TEXT NOT NULL, source_url TEXT NOT NULL,
          size_bytes INTEGER, archive_format TEXT, md5 TEXT, sha1 TEXT, relevant INTEGER NOT NULL DEFAULT 0,
          inventory_seen_at TEXT NOT NULL, download_status TEXT NOT NULL DEFAULT 'not_downloaded',
          local_path TEXT, sha256 TEXT, downloaded_bytes INTEGER, download_error TEXT,
          UNIQUE(identifier,file_name));
        CREATE INDEX IF NOT EXISTS idx_archive_title ON archive_items(title);
        CREATE TABLE IF NOT EXISTS archive_content_assessments(
          identifier TEXT PRIMARY KEY REFERENCES archive_items(identifier) ON DELETE CASCADE,
          scope TEXT NOT NULL DEFAULT 'uncertain',
          rationale TEXT NOT NULL DEFAULT '',
          assessed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS archive_item_contents(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL, title TEXT NOT NULL, creator TEXT NOT NULL DEFAULT '',
          source_file TEXT NOT NULL DEFAULT '', page_range TEXT NOT NULL DEFAULT '',
          evidence TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'candidate',
          notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(identifier, ordinal));
        CREATE TABLE IF NOT EXISTS archive_content_suggestions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          source_file TEXT NOT NULL, line_number INTEGER NOT NULL,
          suggestion_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL, UNIQUE(identifier,source_file,line_number,suggestion_text));
        """)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI Importación", version="0.2.0", lifespan=lifespan)

from app.file_catalog import router as file_catalog_router
app.include_router(file_catalog_router)


def query_for(text: str, scope: str = "all") -> str:
    term = text.strip()
    if not term:
        raise ValueError("Escribe términos para buscar.")
    if scope not in {"all", "texts"}:
        raise ValueError("Ámbito de búsqueda no válido.")
    # Prevent user text from being interpreted as Lucene query syntax.
    term = re.sub(r"[^\w\s]", " ", term, flags=re.UNICODE).strip()
    if not term:
        raise ValueError("La consulta no contiene términos de búsqueda válidos.")
    fields = f'(title:({term}) OR creator:({term}) OR subject:({term}) OR description:({term}))'
    return ("mediatype:texts AND " if scope == "texts" else "") + fields

async def archive_search(query: str, rows: int) -> dict:
    params = [("q", query), ("rows", str(rows)), ("page", "1"), ("output", "json")]
    params.extend(("fl[]", field) for field in FIELDS)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(ARCHIVE_ENDPOINT, params=params)
        response.raise_for_status()
        payload = response.json()
    if payload.get("responseHeader", {}).get("status") != 0:
        raise RuntimeError("Respuesta de búsqueda no válida de Internet Archive")
    data = payload.get("response", {})
    return {"total": int(data.get("numFound", 0)), "docs": data.get("docs", [])}


def display(value):
    if value is None:
        return None
    return "; ".join(map(str, value)) if isinstance(value, list) else str(value)


def save_results(run_id: int, docs: list[dict]) -> int:
    stamp = now()
    with db() as conn:
        for rank, doc in enumerate(docs, 1):
            ident = str(doc.get("identifier", "")).strip()
            if not ident:
                continue
            conn.execute("""INSERT INTO archive_items
            (identifier,title,creator_json,item_date,mediatype,collection_json,rights_json,
             description_json,source_url,metadata_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(identifier) DO UPDATE SET title=excluded.title,creator_json=excluded.creator_json,
             item_date=excluded.item_date,mediatype=excluded.mediatype,collection_json=excluded.collection_json,
             rights_json=excluded.rights_json,description_json=excluded.description_json,
             source_url=excluded.source_url,metadata_json=excluded.metadata_json,last_seen_at=excluded.last_seen_at""",
            (ident, display(doc.get("title")), json.dumps(doc.get("creator"), ensure_ascii=False),
             display(doc.get("date")), display(doc.get("mediatype")), json.dumps(doc.get("collection"), ensure_ascii=False),
             json.dumps(doc.get("rights"), ensure_ascii=False), json.dumps(doc.get("description"), ensure_ascii=False),
             "https://archive.org/details/" + ident, json.dumps(doc, ensure_ascii=False), stamp, stamp))
            conn.execute("INSERT OR REPLACE INTO search_run_items VALUES(?,?,?)", (run_id, ident, rank))
        return int(conn.execute("SELECT COUNT(*) FROM search_run_items WHERE search_run_id=?", (run_id,)).fetchone()[0])


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def page(title: str, body: str) -> HTMLResponse:
    style = """<style>
    body{font:16px system-ui,sans-serif;color:#20242a;background:#f4f6f8;margin:0}
    header{background:#15283f;color:white;padding:1rem max(1rem,calc((100% - 1100px)/2))}
    header a{color:white;margin-right:1.2rem;text-decoration:none}main{max-width:1100px;margin:1.5rem auto;padding:0 1rem}
    .panel{background:white;border:1px solid #dce2e8;border-radius:10px;padding:1rem;margin:1rem 0}
    input,select,button{font:inherit;padding:.65rem;border:1px solid #aab6c4;border-radius:6px}
    button{background:#174f89;color:white;border:0;cursor:pointer}input[type=text]{min-width:240px;width:55%}
    table{border-collapse:collapse;width:100%;background:white}td,th{text-align:left;padding:.65rem;border-bottom:1px solid #e1e6eb;vertical-align:top}
    th{background:#edf1f5}.muted{color:#5d6977;font-size:.9rem}.metrics{display:flex;gap:1rem;flex-wrap:wrap}
    .metric{background:white;border:1px solid #dce2e8;border-radius:8px;padding:1rem;min-width:130px}
    a{color:#175ca8}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f4f6;padding:1rem}
    </style>"""
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>" + esc(title) + "</title>" + style + "<header><b><a href='/'>AI Importación</a></b><a href='/items'>Catálogo local</a><span>Captación independiente · sin altas en OSAP</span></header><main>" + body + "<p class='muted'>La disponibilidad de un archivo no confirma por sí sola que su reutilización esté permitida.</p></main></html>")


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-importacion"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db() as conn:
        counts = [conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM archive_items WHERE review_status='pending'").fetchone()[0]]
        items = conn.execute("SELECT * FROM archive_items ORDER BY last_seen_at DESC LIMIT 20").fetchall()
        runs = conn.execute("SELECT * FROM search_runs ORDER BY id DESC LIMIT 8").fetchall()
    body = "<h1>Captación de partituras</h1><p>Busca por título, compositor u otros términos. Los resultados se guardan en la base de datos local para revisarlos antes de cualquier futura exportación a OSAP.</p>"
    body += "<div class='metrics'>" + "".join(f"<div class='metric'><span class='muted'>{label}</span><h2>{value}</h2></div>" for label, value in zip(["Elementos catalogados","Búsquedas realizadas","Pendientes de revisión"], counts)) + "</div>"
    body += "<section class='panel'><h2>Nueva búsqueda en Internet Archive</h2><form method='post' action='/search'><input name='query' type='text' required placeholder='Bach, Ave Maria, piano sonata'> <label>Buscar en <select name='scope'><option value='all' selected>Todos los tipos de material</option><option value='texts'>Solo textos / libros digitalizados</option></select></label> <label>Resultados <select name='rows'>" + "".join(f"<option {'selected' if n==25 else ''}>{n}</option>" for n in [10,25,50,100]) + "</select></label> <button>Buscar en Internet Archive</button></form><p class='muted'>Busca en metadatos (título, creador, materias y descripción). Por defecto incluye todos los tipos de material; puedes limitar a textos/libros si lo necesitas. La búsqueda de metadatos no lee todavía el contenido de los PDF.</p></section>"
    body += "<section class='panel'><h2>Catálogo local reciente</h2>" + item_table(items) + "<p><a href='/items'>Ver todo el catálogo →</a></p></section>"
    body += "<section class='panel'><h2>Últimas búsquedas</h2><table><tr><th>Consulta</th><th>Resultados</th><th>Estado</th><th>Inicio</th></tr>"
    for run in runs:
        body += f"<tr><td>{esc(run['query_text'])}</td><td>{run['result_count']} guardados / {run['total_found']} encontrados</td><td>{esc(run['status'])} {esc(run['error_text'])}</td><td>{esc(run['started_at'])}</td></tr>"
    body += "</table></section>"
    return page("AI Importación", body)


def item_table(items):
    if not items:
        return "<p>Aún no hay elementos. Ejecuta una búsqueda para empezar a recopilar metadatos.</p>"
    body = "<table><tr><th>Título / identificador</th><th>Creador de origen</th><th>Fecha</th><th>Derechos</th></tr>"
    for item in items:
        body += f"<tr><td><a href='/items/{esc(item['identifier'])}'>{esc(item['title'] or item['identifier'])}</a><br><span class='muted'>{esc(item['identifier'])}</span></td><td>{esc(item['creator_json'])}</td><td>{esc(item['item_date'] or '—')}</td><td>{esc(item['rights_status'])}</td></tr>"
    return body + "</table>"


@app.post("/search", response_class=HTMLResponse)
async def search(query: Annotated[str, Form()], rows: Annotated[int, Form()] = 25, scope: Annotated[str, Form()] = "all"):
    query = query.strip()
    rows = max(1, min(int(rows), 100))
    try:
        archive_query = query_for(query, scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with db() as conn:
        run_id = conn.execute("INSERT INTO search_runs(query_text,archive_query,rows_requested,started_at) VALUES(?,?,?,?)",
                              (query, archive_query, rows, now())).lastrowid
    try:
        result = await archive_search(archive_query, rows)
        saved = save_results(run_id, result["docs"])
        with db() as conn:
            conn.execute("UPDATE search_runs SET total_found=?,result_count=?,status='completed',completed_at=? WHERE id=?",
                         (result["total"], saved, now(), run_id))
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE search_runs SET status='error',error_text=?,completed_at=? WHERE id=?", (str(exc)[:1000], now(), run_id))
        return page("Error de búsqueda", f"<h1>No se pudo completar la búsqueda</h1><p>{esc(exc)}</p><p><a href='/'>Volver</a></p>")
    with db() as conn:
        items = conn.execute("""SELECT ai.* FROM archive_items ai
            JOIN search_run_items sri ON sri.identifier=ai.identifier
            WHERE sri.search_run_id=? ORDER BY sri.result_rank LIMIT 100""", (run_id,)).fetchall()
    body = f"<section class='panel'><h2>Resultado de búsqueda: {esc(query)}</h2><p>Internet Archive informa de <b>{result['total']}</b> coincidencias. Se han recibido y guardado <b>{saved}</b> elementos en esta búsqueda.</p><p><a href='/'>Volver al inicio</a></p></section><section class='panel'><h2>Resultados encontrados</h2>{item_table(items)}</section>"
    return page("Resultado de búsqueda", body)


@app.get("/items", response_class=HTMLResponse)
def items_page(q: str = ""):
    with db() as conn:
        if q.strip():
            like = "%" + q.strip() + "%"
            items = conn.execute("SELECT * FROM archive_items WHERE title LIKE ? OR creator_json LIKE ? OR identifier LIKE ? ORDER BY last_seen_at DESC LIMIT 500", (like, like, like)).fetchall()
        else:
            items = conn.execute("SELECT * FROM archive_items ORDER BY last_seen_at DESC LIMIT 500").fetchall()
    body = "<h1>Catálogo local</h1><form method='get'><input type='text' name='q' value='" + esc(q) + "' placeholder='Título, identificador o creador'> <button>Filtrar</button></form><section class='panel'>" + item_table(items) + "</section>"
    return page("Catálogo local", body)


@app.get("/items/{identifier}", response_class=HTMLResponse)
def item_detail(identifier: str):
    with db() as conn:
        item = conn.execute("SELECT * FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")
    item = dict(item)
    body = f"<p><a href='/items'>← Volver al catálogo</a></p><h1>{esc(item['title'] or identifier)}</h1><p><a target='_blank' rel='noopener' href='{esc(item['source_url'])}'>Abrir elemento original en Internet Archive ↗</a></p>"
    body += "<p><a href=\"/files/" + esc(identifier) + "\">Consultar inventario de ficheros y descargar</a></p>"
    body += "<p><a href='/items/" + esc(identifier) + "/content-analysis'>Analizar si contiene una o varias obras</a></p>"
    body += "<section class='panel'><h2>Ficha local</h2>"
    for label, key in [("Identificador","identifier"),("Creador","creator_json"),("Fecha","item_date"),("Tipo","mediatype"),("Visto por primera vez","first_seen_at"),("Última observación","last_seen_at"),("Declaración de derechos de origen","rights_json")]:
        body += f"<p><b>{label}:</b> {esc(item[key] or '—')}</p>"
    body += f"<p><b>Revisión de derechos:</b> {esc(item['rights_status'])}</p><form method='post' action='/items/{esc(identifier)}/rights-status'><select name='status'>"
    for value, label in [("unreviewed","Sin revisar"),("evidence_found","Evidencia localizada; pendiente de validar"),("public_domain_confirmed","Dominio público confirmado"),("permission_required","Se requiere permiso"),("restricted","Uso restringido"),("unclear","No está claro")]:
        body += f"<option value='{value}' {'selected' if item['rights_status']==value else ''}>{label}</option>"
    body += "</select> <button>Guardar estado</button></form></section><section class='panel'><h2>Metadatos originales conservados</h2><pre>" + esc(json.dumps(json.loads(item["metadata_json"]), ensure_ascii=False, indent=2)) + "</pre></section>"
    return page("Ficha " + identifier, body)


@app.post("/items/{identifier}/rights-status")
def rights_status(identifier: str, status: Annotated[str, Form()] = "unreviewed"):
    allowed = {"unreviewed","evidence_found","public_domain_confirmed","permission_required","restricted","unclear"}
    if status not in allowed:
        raise HTTPException(status_code=422, detail="Estado no válido")
    with db() as conn:
        cur = conn.execute("UPDATE archive_items SET rights_status=? WHERE identifier=?", (status, identifier))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
    return RedirectResponse(url="/items/" + identifier, status_code=303)


CONTENT_SCOPES = {
    "one_work": "Una obra musical",
    "multiple_works": "Varias obras / recopilación de partituras",
    "collection": "Colección o volumen; contenido por determinar",
    "not_music": "No parece contener partituras musicales",
    "uncertain": "Pendiente de determinar",
}


@app.get("/items/{identifier}/content-analysis", response_class=HTMLResponse)
def content_analysis(identifier: str):
    with db() as conn:
        item = conn.execute("SELECT * FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        assessment = conn.execute("SELECT * FROM archive_content_assessments WHERE identifier=?", (identifier,)).fetchone()
        units = conn.execute("SELECT * FROM archive_item_contents WHERE identifier=? ORDER BY ordinal", (identifier,)).fetchall()
        files = conn.execute("SELECT file_name,archive_format,size_bytes,relevant FROM archive_files WHERE identifier=? ORDER BY relevant DESC,file_name", (identifier,)).fetchall()
    scope = assessment["scope"] if assessment else "uncertain"
    rationale = assessment["rationale"] if assessment else ""
    body = f"<p><a href='/items/{esc(identifier)}'>← Volver a la ficha</a></p><h1>Análisis del contenido: {esc(item['title'] or identifier)}</h1>"
    body += "<p>Esta fase determina qué representa el elemento de Internet Archive. Puede ser una sola partitura, un volumen con muchas obras o un contenedor de archivos. Aquí no se crean ni exportan obras OSAP.</p>"
    body += "<section class='panel'><h2>Clasificación del elemento</h2><form method='post' action='/items/" + esc(identifier) + "/content-analysis/scope'><label>¿Qué parece contener? <select name='scope'>"
    for value, label in CONTENT_SCOPES.items():
        body += f"<option value='{value}' {'selected' if scope == value else ''}>{label}</option>"
    body += f"</select></label><p><label>Justificación / evidencias consultadas<br><textarea name='rationale' rows='3' style='width:95%'>{esc(rationale)}</textarea></label></p><button>Guardar clasificación</button></form></section>"
    body += "<section class='panel'><h2>Ficheros conocidos</h2>"
    if not files:
        body += "<p>No hay inventario de ficheros todavía. <a href='/files/" + esc(identifier) + "'>Consultar el inventario</a> para identificar PDF, MusicXML, MIDI, ZIP o imágenes.</p>"
    else:
        body += "<table><tr><th>Nombre</th><th>Formato</th><th>Tamaño</th><th>Candidato</th></tr>"
        for frow in files:
            body += f"<tr><td>{esc(frow['file_name'])}</td><td>{esc(frow['archive_format'] or '—')}</td><td>{esc(frow['size_bytes'] if frow['size_bytes'] is not None else '—')}</td><td>{'Sí' if frow['relevant'] else 'No'}</td></tr>"
        body += "</table>"
    body += "<p class='muted'>La lista de ficheros es una pista, no una prueba de cuántas obras contiene el elemento.</p></section>"
    body += "<section class='panel'><h2>Ayuda automática desde OCR / texto</h2><p>La aplicación puede leer ficheros de texto OCR descargados y extraer texto de PDF que permitan copiarlo. Propone líneas que podrían ser títulos; no decide que sean obras válidas ni crea candidatas hasta que las aceptes.</p><form method='post' action='/items/" + esc(identifier) + "/content-analysis/extract'><button>Extraer sugerencias de ficheros descargados</button></form>";
    with db() as conn:
        suggestions = conn.execute("SELECT * FROM archive_content_suggestions WHERE identifier=? ORDER BY source_file,line_number,id", (identifier,)).fetchall()
    if suggestions:
        body += "<h3>Sugerencias para revisar</h3><table><tr><th>Texto detectado</th><th>Origen</th><th>Estado / acción</th></tr>"
        for suggestion in suggestions:
            body += f"<tr><td>{esc(suggestion['suggestion_text'])}</td><td>{esc(suggestion['source_file'])}<br>línea/página {suggestion['line_number']}</td><td>{esc(suggestion['status'])}"
            if suggestion["status"] == "pending":
                body += f"<form method='post' action='/items/{esc(identifier)}/content-analysis/suggestions/{suggestion['id']}/accept'><button>Aceptar como obra candidata</button></form>"
            body += "</td></tr>"
        body += "</table>"
    body += "</section>"
    body += "<section class='panel'><h2>Obras o unidades de contenido identificadas</h2><p>Registra cada obra visible dentro de un PDF, volumen o conjunto. El fichero y las páginas son opcionales, pero ayudan a localizarla después.</p>"
    body += "<form method='post' action='/items/" + esc(identifier) + "/content-analysis/units'><p><label>Título de la obra o unidad *<br><input type='text' name='title' required></label></p><p><label>Compositor / creador<br><input type='text' name='creator'></label></p><p><label>Fichero de origen<br><input type='text' name='source_file'></label></p><p><label>Páginas / rango / localizador<br><input type='text' name='page_range' placeholder='p. 1–8, pistas 3–5, páginas del PDF'></label></p><p><label>Evidencia (índice, portada, contenido, OCR...)<br><input type='text' name='evidence' style='width:90%'></label></p><p><label>Notas<br><textarea name='notes' rows='2' style='width:95%'></textarea></label></p><button>Añadir obra candidata</button></form>"
    if units:
        body += "<h3>Contenido registrado</h3><table><tr><th>#</th><th>Título / creador</th><th>Localizador</th><th>Evidencia / notas</th><th>Estado</th></tr>"
        for unit in units:
            body += f"<tr><td>{unit['ordinal']}</td><td><b>{esc(unit['title'])}</b><br>{esc(unit['creator'] or 'Creador desconocido')}</td><td>{esc(unit['source_file'])}<br>{esc(unit['page_range'])}</td><td>{esc(unit['evidence'])}<br>{esc(unit['notes'])}</td><td>{esc(unit['status'])}</td></tr>"
        body += "</table>"
    else:
        body += "<p>Todavía no se han registrado obras candidatas. No se infiere automáticamente que el elemento equivalga a una sola obra.</p>"
    body += "</section>"
    return page("Análisis de contenido", body)


@app.post("/items/{identifier}/content-analysis/scope")
def save_content_scope(identifier: str, scope: Annotated[str, Form()], rationale: Annotated[str, Form()] = ""):
    if scope not in CONTENT_SCOPES:
        raise HTTPException(status_code=422, detail="Clasificación no válida")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM archive_items WHERE identifier=?", (identifier,)).fetchone():
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        conn.execute("""INSERT INTO archive_content_assessments(identifier,scope,rationale,assessed_at)
            VALUES(?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET scope=excluded.scope,
            rationale=excluded.rationale,assessed_at=excluded.assessed_at""",
            (identifier, scope, rationale.strip()[:5000], now()))
    return RedirectResponse(url="/items/" + identifier + "/content-analysis", status_code=303)


@app.post("/items/{identifier}/content-analysis/units")
def add_content_unit(
    identifier: str, title: Annotated[str, Form()], creator: Annotated[str, Form()] = "",
    source_file: Annotated[str, Form()] = "", page_range: Annotated[str, Form()] = "",
    evidence: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = "",
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="El título es obligatorio")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM archive_items WHERE identifier=?", (identifier,)).fetchone():
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        ordinal = int(conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM archive_item_contents WHERE identifier=?", (identifier,)).fetchone()[0])
        conn.execute("""INSERT INTO archive_item_contents
            (identifier,ordinal,title,creator,source_file,page_range,evidence,notes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (identifier, ordinal, title[:1000], creator.strip()[:500], source_file.strip()[:1000],
             page_range.strip()[:500], evidence.strip()[:2000], notes.strip()[:3000], now()))
        count = int(conn.execute("SELECT COUNT(*) FROM archive_item_contents WHERE identifier=?", (identifier,)).fetchone()[0])
        assessment = conn.execute("SELECT scope FROM archive_content_assessments WHERE identifier=?", (identifier,)).fetchone()
        if count >= 2 and (assessment is None or assessment["scope"] == "uncertain"):
            conn.execute("""INSERT INTO archive_content_assessments(identifier,scope,rationale,assessed_at)
                VALUES(?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET scope=excluded.scope,assessed_at=excluded.assessed_at""",
                (identifier, "multiple_works", "Clasificación sugerida al registrar dos o más obras candidatas; requiere revisión humana.", now()))
    return RedirectResponse(url="/items/" + identifier + "/content-analysis", status_code=303)


def extract_text_candidates(text: str) -> list[tuple[int, str]]:
    """Conservative title-like lines from OCR/text; suggestions always require human review."""
    ignored = ("internet archive", "table of contents", "contents", "copyright", "all rights reserved",
               "public domain", "digitized by", "scanned by", "www.", "http://", "https://")
    candidates = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = " ".join(raw.replace("\x0c", " ").split()).strip(" .·—-")
        if not 4 <= len(line) <= 140:
            continue
        low = line.lower()
        if any(token in low for token in ignored):
            continue
        if re.fullmatch(r"[\W_\d]+", line) or len(re.findall(r"\d", line)) > len(line) * 0.35:
            continue
        if len(line.split()) > 18 or line.endswith((":", ";", ",")):
            continue
        # Contents pages often end in dotted leaders and a page number.
        line = re.sub(r"\s*[.·_]{2,}\s*\d{1,4}\s*$", "", line).strip(" .·—-")
        line = re.sub(r"^\s*(?:no?\.?\s*)?\d{1,3}[.)]\s*", "", line).strip()
        if 4 <= len(line) <= 140 and line not in {value for _, value in candidates}:
            candidates.append((line_no, line))
    return candidates[:300]


@app.post("/items/{identifier}/content-analysis/extract")
def extract_content_suggestions(identifier: str):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM archive_items WHERE identifier=?", (identifier,)).fetchone():
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        files = conn.execute("""SELECT file_name,local_path FROM archive_files
            WHERE identifier=? AND download_status='downloaded' AND local_path IS NOT NULL""", (identifier,)).fetchall()
    if not files:
        raise HTTPException(status_code=422, detail="Primero consulta el inventario y descarga un fichero de texto OCR o un PDF.")
    examined = 0
    inserted = 0
    for frow in files:
        path = Path(frow["local_path"])
        if not path.exists():
            continue
        text_content = ""
        try:
            if path.suffix.lower() == ".txt":
                text_content = path.read_text(encoding="utf-8", errors="replace")
            elif path.suffix.lower() == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(str(path))
                pages = []
                for page_no, pdf_page in enumerate(reader.pages, 1):
                    page_text = pdf_page.extract_text() or ""
                    pages.append(f"\n[PAGE {page_no}]\n" + page_text)
                text_content = "\n".join(pages)
        except Exception:
            continue
        if not text_content.strip():
            continue
        examined += 1
        with db() as conn:
            for line_no, candidate in extract_text_candidates(text_content):
                cur = conn.execute("""INSERT OR IGNORE INTO archive_content_suggestions
                    (identifier,source_file,line_number,suggestion_text,status,created_at)
                    VALUES(?,?,?,?, 'pending', ?)""",
                    (identifier, frow["file_name"], line_no, candidate, now()))
                inserted += cur.rowcount
    return RedirectResponse(url="/items/" + identifier + "/content-analysis", status_code=303)


@app.post("/items/{identifier}/content-analysis/suggestions/{suggestion_id}/accept")
def accept_content_suggestion(identifier: str, suggestion_id: int):
    with db() as conn:
        suggestion = conn.execute("""SELECT * FROM archive_content_suggestions
            WHERE id=? AND identifier=? AND status='pending'""", (suggestion_id, identifier)).fetchone()
        if not suggestion:
            raise HTTPException(status_code=404, detail="Sugerencia pendiente no encontrada")
        ordinal = int(conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM archive_item_contents WHERE identifier=?", (identifier,)).fetchone()[0])
        conn.execute("""INSERT INTO archive_item_contents
            (identifier,ordinal,title,source_file,page_range,evidence,notes,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (identifier, ordinal, suggestion["suggestion_text"], suggestion["source_file"],
             f"línea {suggestion['line_number']}", "Sugerencia automática extraída de texto/OCR; aceptada por una persona.",
             "Revisar el título y asignar compositor si se conoce.", now()))
        conn.execute("UPDATE archive_content_suggestions SET status='accepted' WHERE id=?", (suggestion_id,))
    return RedirectResponse(url="/items/" + identifier + "/content-analysis", status_code=303)
