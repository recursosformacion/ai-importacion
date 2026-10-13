from __future__ import annotations

import html
import json
import os
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
        CREATE INDEX IF NOT EXISTS idx_archive_title ON archive_items(title);
        """)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI Importación", version="0.1.0", lifespan=lifespan)


def query_for(text: str) -> str:
    phrase = text.strip().replace("\\", "\\\\").replace('"', '\\"')
    if not phrase:
        raise ValueError("Escribe términos para buscar.")
    fields = f'(title:"{phrase}" OR creator:"{phrase}" OR subject:"{phrase}" OR description:"{phrase}")'
    return "mediatype:texts AND " + fields


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
    body += "<section class='panel'><h2>Nueva búsqueda</h2><form method='post' action='/search'><input name='query' type='text' required placeholder='Bach, Ave Maria, piano sonata'> <label>Resultados <select name='rows'>" + "".join(f"<option {'selected' if n==25 else ''}>{n}</option>" for n in [10,25,50,100]) + "</select></label> <button>Buscar en Internet Archive</button></form><p class='muted'>Busca en títulos, creadores, materias y descripciones de elementos de tipo texto.</p></section>"
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
async def search(query: Annotated[str, Form()], rows: Annotated[int, Form()] = 25):
    query = query.strip()
    rows = max(1, min(int(rows), 100))
    try:
        archive_query = query_for(query)
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
        items = conn.execute("SELECT * FROM archive_items ORDER BY last_seen_at DESC LIMIT 50").fetchall()
    body = f"<section class='panel'><h2>Resultado de búsqueda</h2><p>Internet Archive informa de <b>{result['total']}</b> coincidencias. Se han recibido y guardado <b>{saved}</b> elementos.</p><p><a href='/'>Volver al inicio</a></p></section><section class='panel'><h2>Catálogo local</h2>{item_table(items)}</section>"
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
