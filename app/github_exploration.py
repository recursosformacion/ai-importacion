from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import connect

router = APIRouter()
DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
API = "https://api.github.com"
def load_github_token():
    """Load a private token from the environment or a local, git-ignored file."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        return token

    root = Path(__file__).resolve().parents[1]
    for filename in ("secrets", "secrets.txt"):
        path = root / filename
        try:
            content = path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if not content:
            continue

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            candidate = ""
            if "=" in line:
                key, value = line.split("=", 1)
                if key.strip().lower() not in {"github_token", "github.token", "github-token", "token"}:
                    continue
                candidate = value.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                if key.strip().lower() not in {"github_token", "github.token", "github-token", "token"}:
                    continue
                candidate = value.strip()
            else:
                candidate = line
            candidate = candidate.strip().strip('"').strip("'")
            if candidate:
                return candidate
    return ""


TOKEN = load_github_token()
MIN_MUSICAL_FILES = 11
PER_PAGE = 100
MAX_SEARCH_PAGES = 10
QUERIES = [
    "music", "sheet music", "musical scores", "musicxml", "midi",
    "lilypond", "abc notation", "humdrum", "open scores",
    "public domain music", "choral scores", "classical scores",
    "score library", "music notation", "mei music",
]
EXTENSIONS = {
    ".musicxml", ".mxl", ".mid", ".midi", ".kar", ".krn", ".ly", ".ily",
    ".abc", ".mei", ".mml", ".mscx", ".mscz", ".capx", ".sib", ".musx",
    ".gp", ".gp3", ".gp4", ".gp5", ".gpx", ".ptb", ".nwc", ".nwctxt",
    ".nwz", ".bww", ".ove", ".enc", ".mgu", ".sgu", ".not", ".mp3",
    ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".aiff", ".aif",
    ".wma", ".ape", ".alac",
}
SEEDS = [
    "OpenScore/Lieder", "OpenScore/StringQuartets", "mutopia/MutopiaProject",
    "musetrainer/library",
]

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GitHubRateLimit(RuntimeError):
    def __init__(self, message, resume_after):
        super().__init__(message)
        self.resume_after = resume_after

def esc(v):
    return html.escape("" if v is None else str(v))

def db():
    return connect(DB_PATH)

def ensure_schema():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS github_discovery_queries(
          query TEXT PRIMARY KEY, page INTEGER NOT NULL DEFAULT 1,
          total_count INTEGER, status TEXT NOT NULL DEFAULT 'pending',
          last_error TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS github_discovered_repositories(
          full_name TEXT PRIMARY KEY, html_url TEXT NOT NULL DEFAULT '',
          default_branch TEXT NOT NULL DEFAULT 'main', description TEXT NOT NULL DEFAULT '',
          stars INTEGER NOT NULL DEFAULT 0, license_spdx TEXT NOT NULL DEFAULT '',
          discovery_query TEXT NOT NULL DEFAULT '', discovered_at TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
          musical_file_count INTEGER NOT NULL DEFAULT 0, tree_sha TEXT NOT NULL DEFAULT '',
          last_checked_at TEXT, last_error TEXT, below_threshold INTEGER NOT NULL DEFAULT 0, commit_sha TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_github_discovered_status
          ON github_discovered_repositories(status, discovered_at);
        CREATE TABLE IF NOT EXISTS github_exploration_state(
          id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
          last_run_at TEXT, last_run_seconds REAL NOT NULL DEFAULT 0,
          last_message TEXT NOT NULL DEFAULT '', resume_after TEXT);
        INSERT OR IGNORE INTO github_exploration_state(id) VALUES(1);
        """)
        state_cols = {r["name"] for r in c.execute("PRAGMA table_info(github_exploration_state)")}
        if "resume_after" not in state_cols:
            c.execute("ALTER TABLE github_exploration_state ADD COLUMN resume_after TEXT")
        if "job_status" not in state_cols:
            c.execute("ALTER TABLE github_exploration_state ADD COLUMN job_status TEXT NOT NULL DEFAULT 'idle'")
        if "requested_minutes" not in state_cols:
            c.execute("ALTER TABLE github_exploration_state ADD COLUMN requested_minutes INTEGER NOT NULL DEFAULT 60")
        if "progress_discoveries" not in state_cols:
            c.execute("ALTER TABLE github_exploration_state ADD COLUMN progress_discoveries INTEGER NOT NULL DEFAULT 0")
        if "progress_inspections" not in state_cols:
            c.execute("ALTER TABLE github_exploration_state ADD COLUMN progress_inspections INTEGER NOT NULL DEFAULT 0")

        # One-time clean reset of the old persistent exploration state.
        # The separate GitHub catalog, downloaded files and analyses are untouched.
        c.execute("""CREATE TABLE IF NOT EXISTS github_exploration_migrations(
          migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
        reset_done = c.execute(
            "SELECT 1 FROM github_exploration_migrations WHERE migration_key=?",
            ("reset_exploration_before_token_auth",),
        ).fetchone()
        if not reset_done:
            c.execute("DELETE FROM github_discovery_queries")
            c.execute("DELETE FROM github_discovered_repositories")
            c.execute("""UPDATE github_exploration_state
              SET enabled=0,last_run_at=NULL,last_run_seconds=0,
                  last_message='Exploración reiniciada para comenzar un catálogo limpio.',
                  resume_after=NULL WHERE id=1""")
            c.execute(
                "INSERT INTO github_exploration_migrations(migration_key,applied_at) VALUES(?,?)",
                ("reset_exploration_before_token_auth", now()),
            )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(github_discovered_repositories)")}
        if "commit_sha" not in cols:
            c.execute("ALTER TABLE github_discovered_repositories ADD COLUMN commit_sha TEXT NOT NULL DEFAULT ''")
        for q in QUERIES:
            c.execute("INSERT OR IGNORE INTO github_discovery_queries(query,updated_at) VALUES(?,?)",(q,now()))
        for name in SEEDS:
            c.execute("""INSERT OR IGNORE INTO github_discovered_repositories
              (full_name,html_url,discovery_query,discovered_at,status)
              VALUES(?,?, 'seed', ?, 'pending')""",
              (name,"https://github.com/"+name,now()))
        # Reclassify the catalog only once per threshold policy, not on every
        # page view, status poll, or application restart.
        threshold_migration = "repository_threshold_11_v2"
        if not c.execute(
            "SELECT 1 FROM github_exploration_migrations WHERE migration_key=?",
            (threshold_migration,),
        ).fetchone():
            c.execute("""UPDATE github_discovered_repositories
              SET status='qualified',below_threshold=0
              WHERE status='below_threshold' AND musical_file_count>=?
                AND last_checked_at IS NOT NULL""", (MIN_MUSICAL_FILES,))
            c.execute("""UPDATE github_discovered_repositories
              SET status='below_threshold',below_threshold=1
              WHERE status IN ('qualified','partial')
                AND musical_file_count < ? AND last_checked_at IS NOT NULL""",
              (MIN_MUSICAL_FILES,))
            c.execute(
                "INSERT INTO github_exploration_migrations(migration_key,applied_at) VALUES(?,?)",
                (threshold_migration, now()),
            )

def headers():
    h={"Accept":"application/vnd.github+json","User-Agent":"OSAP-AI-Importacion/0.2",
       "X-GitHub-Api-Version":"2022-11-28"}
    if TOKEN: h["Authorization"]="Bearer "+TOKEN
    return h

async def get_json(client,url):
    r=await client.get(url)
    if r.status_code in (403, 429):
        body = (r.text or "").lower()
        remaining = r.headers.get("X-RateLimit-Remaining")
        retry_after = r.headers.get("Retry-After")
        reset = r.headers.get("X-RateLimit-Reset")
        limited = (remaining == "0" or bool(retry_after) or
                   "rate limit" in body or "secondary rate" in body)
        if limited:
            resume_after = None
            try:
                if retry_after:
                    resume_after = datetime.fromtimestamp(
                        time.time() + max(1, int(retry_after)), timezone.utc
                    ).isoformat(timespec="seconds")
                elif reset:
                    resume_after = datetime.fromtimestamp(int(reset), timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError, OverflowError):
                resume_after = None
            if not resume_after:
                resume_after = datetime.fromtimestamp(
                    time.time() + 60, timezone.utc
                ).isoformat(timespec="seconds")
            raise GitHubRateLimit(
                "GitHub ha aplicado un límite de solicitudes. El avance queda guardado; "
                f"se podrá reanudar después de {resume_after}.",
                resume_after,
            )
    r.raise_for_status()
    return r.json()

async def discover_one(client):
    with db() as c:
        q=c.execute("""SELECT * FROM github_discovery_queries
          WHERE status IN ('pending','running') AND page<=?
          ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, query LIMIT 1""",
          (MAX_SEARCH_PAGES,)).fetchone()
    if not q:
        return False, "No quedan páginas de búsqueda pendientes."
    query=q["query"]; page=int(q["page"])
    params=urlencode({"q":query+" fork:false","sort":"updated","order":"desc",
                      "per_page":PER_PAGE,"page":page})
    payload=await get_json(client,f"{API}/search/repositories?{params}")
    items=payload.get("items") or []
    stamp=now()
    with db() as c:
        c.execute("""UPDATE github_discovery_queries SET total_count=?,page=?,status=?,
          last_error=NULL,updated_at=? WHERE query=?""",
          (int(payload.get("total_count") or 0),page+1,
           "done" if page>=MAX_SEARCH_PAGES or len(items)<PER_PAGE else "running",stamp,query))
        for item in items:
            full=item.get("full_name")
            if not full: continue
            lic=item.get("license") or {}
            c.execute("""INSERT INTO github_discovered_repositories
              (full_name,html_url,default_branch,description,stars,license_spdx,
               discovery_query,discovered_at,updated_at,status)
              VALUES(?,?,?,?,?,?,?,?,?,'pending')
              ON CONFLICT(full_name) DO UPDATE SET
              html_url=excluded.html_url, default_branch=excluded.default_branch,
              description=excluded.description, stars=excluded.stars,
              license_spdx=excluded.license_spdx, updated_at=excluded.updated_at""",
              (full,item.get("html_url") or "",item.get("default_branch") or "main",
               item.get("description") or "",int(item.get("stargazers_count") or 0),
               lic.get("spdx_id") or "",query,stamp,item.get("updated_at") or ""))
    return True,f"Consulta «{query}», página {page}: {len(items)} repositorios recibidos."

async def inspect_one(client):
    with db() as c:
        repo=c.execute("""SELECT * FROM github_discovered_repositories
          WHERE status='pending' ORDER BY
          CASE discovery_query WHEN 'seed' THEN 0 ELSE 1 END, discovered_at LIMIT 1""").fetchone()
    if not repo: return False,"No quedan repositorios pendientes de inspección."
    name=repo["full_name"]
    try:
        meta=await get_json(client,f"{API}/repos/{name}")
        branch=meta.get("default_branch") or "main"
        commit=await get_json(client,f"{API}/repos/{name}/commits/{quote(branch,safe='')}")
        commit_sha=(commit.get("sha") or "")
        tree_sha=((commit.get("commit") or {}).get("tree") or {}).get("sha","")
        tree=await get_json(client,f"{API}/repos/{name}/git/trees/{quote(branch,safe='')}?recursive=1")
        entries=tree.get("tree") or []
        count=sum(1 for e in entries if e.get("type")=="blob" and
          os.path.splitext(str(e.get("path") or ""))[1].lower() in EXTENSIONS)
        truncated=bool(tree.get("truncated"))
        status="qualified" if count>=MIN_MUSICAL_FILES else ("partial" if truncated else "below_threshold")
        with db() as c:
            c.execute("""UPDATE github_discovered_repositories SET default_branch=?,
              html_url=?,description=?,stars=?,license_spdx=?,musical_file_count=?,
              tree_sha=?,last_checked_at=?,updated_at=?,status=?,below_threshold=?,
              commit_sha=?,last_error=NULL WHERE full_name=?""",
              (branch,meta.get("html_url") or repo["html_url"],meta.get("description") or "",
               int(meta.get("stargazers_count") or 0),
               ((meta.get("license") or {}).get("spdx_id") or ""),count,tree_sha,
               now(),meta.get("updated_at") or "",status,int(count<MIN_MUSICAL_FILES),commit_sha,name))
        return True,f"{name}: {count} archivos musicales"+(" (árbol parcial)" if truncated else "")+"."
    except GitHubRateLimit:
        # A rate limit is a pause, not a repository inspection failure.
        raise
    except Exception as exc:
        with db() as c:
            c.execute("UPDATE github_discovered_repositories SET status='error',last_checked_at=?,last_error=? WHERE full_name=?",
                      (now(),str(exc)[:500],name))
        return True,f"{name}: error: {str(exc)[:160]}"

@router.get("/github", include_in_schema=False)
def github_main():
    return RedirectResponse("/github/exploration", status_code=303)


@router.get("/github/exploration",response_class=HTMLResponse)
def exploration_home(page: int = 1, status: str = "", q: str = "",
                    catalog_added: int = 0, processing_started: int = 0, catalog_skipped: int = 0):
    ensure_schema()
    from app import github_catalog, github_processing
    github_catalog.DB_PATH = DB_PATH
    github_processing.DB_PATH = DB_PATH
    github_catalog.ensure_schema()
    github_processing.ensure_processing_schema()
    page = max(1, int(page or 1))
    status = status if status in {"pending","qualified","below_threshold","partial","error","ignored"} else ""
    q = q.strip()[:120]
    page_size = 100
    # La tabla solo presenta repositorios cuyo recuento ya alcanza el mínimo.
    # Los candidatos aún no inspeccionados y los que quedan por debajo se
    # conservan en la base de datos, pero no se ofrecen como repositorios válidos.
    where = ["musical_file_count >= ?"]
    params = [MIN_MUSICAL_FILES]
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append("(full_name LIKE ? OR description LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as c:
        counts={r["status"]:r["n"] for r in c.execute(
          "SELECT status,COUNT(*) n FROM github_discovered_repositories GROUP BY status")}
        q_pending=c.execute("SELECT COUNT(*) n FROM github_discovery_queries WHERE status!='done'").fetchone()["n"]
        state=c.execute("SELECT * FROM github_exploration_state WHERE id=1").fetchone()
        filtered_total=c.execute("SELECT COUNT(*) n FROM github_discovered_repositories"+clause,params).fetchone()["n"]
        repositories=c.execute("""SELECT d.full_name,d.description,d.status,d.musical_file_count,
          d.updated_at,d.last_checked_at,d.last_error,
          (SELECT id FROM github_repositories r WHERE r.full_name=d.full_name) AS catalog_id,
          (SELECT p.status FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS processing_status,
          (SELECT p.error_text FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS processing_error,
          (SELECT p.stage FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS processing_stage,
          (SELECT p.progress_count FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS progress_count,
          (SELECT p.total_count FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS total_count,
          (SELECT p.processed_count FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS processed_count,
          (SELECT p.current_path FROM github_processing_jobs p JOIN github_repositories r ON r.id=p.repository_id WHERE r.full_name=d.full_name) AS current_path,
          (SELECT COUNT(*) FROM github_files f JOIN github_repositories r ON r.id=f.repository_id WHERE r.full_name=d.full_name AND f.extension<>'.txt') AS inventory_count,
          (SELECT COUNT(*) FROM github_files f JOIN github_repositories r ON r.id=f.repository_id WHERE r.full_name=d.full_name AND f.extension<>'.txt' AND f.download_status='downloaded') AS downloaded_count,
          (SELECT COUNT(DISTINCT f.id) FROM github_files f JOIN github_repositories r ON r.id=f.repository_id LEFT JOIN github_file_analyses a ON a.github_file_id=f.id LEFT JOIN github_content_extractions x ON x.github_file_id=f.id WHERE r.full_name=d.full_name AND (a.github_file_id IS NOT NULL OR x.github_file_id IS NOT NULL)) AS analyzed_count
          FROM github_discovered_repositories d"""+clause.replace("musical_file_count","d.musical_file_count").replace("status=?","d.status=?").replace("full_name LIKE","d.full_name LIKE").replace("description LIKE","d.description LIKE")+"""
          ORDER BY d.musical_file_count DESC, d.full_name COLLATE NOCASE ASC
          LIMIT ? OFFSET ?""",params+[page_size,(page-1)*page_size]).fetchall()
    total=sum(counts.values())
    body="<h1>Fuentes GitHub</h1>"
    if catalog_added or catalog_skipped:
        body+="<section class='panel'><b>Incorporación y procesamiento:</b> "+str(catalog_added)+" repositorios incorporados; "+str(processing_started)+" repositorios en cola de procesamiento. Se procesan de uno en uno y el JSON se genera automáticamente al terminar."
        if catalog_skipped:
            body+=" "+str(catalog_skipped)+" omitidos porque no alcanzan el mínimo de "+str(MIN_MUSICAL_FILES)+" archivos musicales inspeccionados."
        body+="</section>"
    body+="<section class='panel'><h2>1. Buscar repositorios</h2><p>Descubrir colecciones musicales en GitHub y guardar los resultados.</p>"
    body+="<form method='post' action='/github/exploration/run'><label>Duración máxima de búsqueda (minutos) <input type='number' name='minutes' min='1' max='1440' value='"+str(state["requested_minutes"] or 240)+"'></label> "
    body+="<p class='muted'>Se detiene antes si no quedan búsquedas o repositorios pendientes, o si GitHub aplica un límite de solicitudes.</p>"
    body+="<button"+(" disabled" if state["job_status"] in ("queued","running") else "")+">Buscar repositorios</button></form>"
    body+="<div id='exploration-live'><b>Estado:</b> <span id='job-status'>"+esc(state["job_status"] or "idle")+"</span><p id='job-progress'>Páginas: "+str(state["progress_discoveries"])+" · Repositorios inspeccionados: "+str(state["progress_inspections"])+"</p><p id='job-message'>"+esc(state["last_message"])+"</p></div></section>"
    body+="<section class='panel'><h2>2. Seleccionar repositorios</h2><p>Elegir qué colecciones se quieren incorporar. Inventariar, descargar agrupadamente y analizar los ficheros. Construir el catálogo a partir de los datos almacenados en SQLite. Ver el estado, las cantidades y obtener el JSON generado.</p>"
    body+="<p>Se muestran "+str(len(repositories))+" de "+str(filtered_total)+" repositorios que cumplen el criterio de más de 10 archivos musicales.</p>"
    body+="<form method='get' action='/github/exploration'><label>Buscar <input name='q' value='"+esc(q)+"' placeholder='Propietario, repositorio o descripción'></label> "
    body+="<label>Estado <select name='status'><option value=''>Todos</option>"
    for value,label in (("pending","Pendiente"),("qualified","Cumple mínimo"),("below_threshold","Por debajo del mínimo"),("partial","Árbol parcial"),("error","Error"),("ignored","Ignorado")):
        body+="<option value='"+value+"'"+(" selected" if status==value else "")+">"+label+"</option>"
    body+="</select></label> <button>Filtrar</button> <a href='/github/exploration'>Limpiar</a></form>"
    if repositories:
        body+="<form method='post' action='/github/exploration/repositories/action'>"
        body+="<input type='hidden' name='return_page' value='"+str(page)+"'><input type='hidden' name='return_status' value='"+esc(status)+"'><input type='hidden' name='return_q' value='"+esc(q)+"'>"
        body+="<p><label><input type='checkbox' data-select-visible> Seleccionar todos los visibles</label> · Seleccionados: <b id='repo-selection-count'>0</b> "
        body+="<button name='action' value='remove'>Quitar seleccionados de la lista</button></p>"
        body+="<table><tr><th>Quitar</th><th>Repositorio</th><th>Archivos musicales</th><th>Proceso</th><th>Información</th></tr>"
        for r in repositories:
            status_label={"pending":"Pendiente","qualified":"Cumple mínimo","below_threshold":"Por debajo del mínimo","partial":"Árbol parcial","error":"Error","ignored":"Ignorado"}.get(r["status"],r["status"])
            detail=("<div class='muted'>"+esc(r["last_error"])+"</div>") if r["last_error"] else ""
            body+="<tr data-processing='"+esc(r["processing_status"] or "")+"'><td><input class='repo-select' type='checkbox' name='repositories' value='"+esc(r["full_name"])+"'></td>"
            body+="<td><a href='https://github.com/"+esc(r["full_name"])+"'>"+esc(r["full_name"])+"</a><div class='muted'>"+esc(r["description"])+"</div>"+detail+"</td>"
            count_cell = str(r["musical_file_count"])
            admission = ("Sí" if r["status"] in ("qualified","partial") else
                         "Pendiente de reinspección" if r["status"] == "pending" else "No disponible")
            body+="<td><b>"+esc(count_cell)+"</b></td><td>"
            if not r["catalog_id"] or r["processing_status"]=="error":
                if r["status"] in ("qualified","partial"):
                    button_label="Reintentar" if r["catalog_id"] else "Seleccionar"
                    body+="<button type='submit' name='full_name' value='"+esc(r["full_name"])+"' formaction='/github/exploration/repository/incorporate' formmethod='post' formnovalidate>"+button_label+"</button>"
                else:
                    body+=esc(status_label)
            else:
                pstatus=r["processing_status"] or "pendiente"
                body+="<b>"+esc(pstatus)+"</b><div class='muted'>"+esc(r["processing_stage"] or "En cola")+" · Inventariados: "+str(r["inventory_count"] or 0)+" · Descargados: "+str(r["downloaded_count"] or 0)+" · Analizados: "+str(r["analyzed_count"] or 0)+"</div>"
                if r["processing_stage"]=="analysis" and r["total_count"]:
                    pct=min(100,int((r["progress_count"] or 0)*100/r["total_count"]))
                    body+="<progress max='100' value='"+str(pct)+"'></progress> "+str(r["progress_count"] or 0)+"/"+str(r["total_count"])
                    if r["current_path"]:
                        body+="<div class='muted'>Analizando: "+esc(r["current_path"])+"</div>"
                elif pstatus=="running" and r["processing_stage"]=="download" and r["total_count"]:
                    pct=min(100,int((r["progress_count"] or 0)*100/r["total_count"]))
                    body+="<progress max='100' value='"+str(pct)+"'></progress> "+str(r["progress_count"] or 0)+"/"+str(r["total_count"])+" archivos adquiridos"
                    if r["current_path"]:
                        body+="<div class='muted'>Descargando: "+esc(r["current_path"])+"</div>"
                elif pstatus in ("running","queued"):
                    stage_label={"inventory":"Inventariando archivos","download":"Descargando archivos agrupadamente","analysis":"Analizando archivos musicales","export":"Generando resources.json","queued":"En cola"}.get(r["processing_stage"] or pstatus,"Procesando")
                    body+="<progress></progress><div class='muted'>"+stage_label+"</div>"
                if pstatus=="completed":
                    body+="<div><a href='/github/"+str(r["catalog_id"])+"/resources.json'>Descargar resources.json ("+str(r["processed_count"] or 0)+" recursos)</a></div>"
            body+="</td><td>"+esc(r["processing_error"] or r["last_error"] or "—")+"</td></tr>"
        body+="</table></form>"
        pages=max(1,(filtered_total+page_size-1)//page_size)
        body+="<p class='pagination'>"
        if page>1: body+="<a href='/github/exploration?page="+str(page-1)+"&status="+esc(status)+"&q="+quote(q)+"'>← Anterior</a> "
        body+="Página "+str(page)+" de "+str(pages)
        if page<pages: body+=" <a href='/github/exploration?page="+str(page+1)+"&status="+esc(status)+"&q="+quote(q)+"'>Siguiente →</a>"
        body+="</p>"
    else:
        body+="<p>No hay repositorios que cumplan el criterio por ahora.</p>"
    body+="</section><p><a href='/github'>Volver a Fuentes GitHub</a></p>"
    live_script = """<script>
(function(){
 const selectionKey='ai-importacion.github-exploration.selected.v1';
 const selected=new Set();
 try{
  const saved=JSON.parse(localStorage.getItem(selectionKey)||'[]');
  if(Array.isArray(saved)) saved.forEach(value=>{if(typeof value==='string')selected.add(value);});
 }catch(e){}
 const boxes=Array.from(document.querySelectorAll('input.repo-select'));
 boxes.forEach(box=>{
  box.checked=selected.has(box.value);
  box.addEventListener('change',()=>{
   if(box.checked)selected.add(box.value);else selected.delete(box.value);
   persist();
  });
 });
 function persist(){
  try{localStorage.setItem(selectionKey,JSON.stringify(Array.from(selected)));}catch(e){}
  const counter=document.getElementById('repo-selection-count');
  if(counter)counter.textContent=String(selected.size);
 }
 const selectAll=document.querySelector('input[data-select-visible]');
 if(selectAll){
  selectAll.checked=boxes.length>0&&boxes.every(box=>box.checked);
  selectAll.addEventListener('change',()=>{
   boxes.forEach(box=>{box.checked=selectAll.checked;if(box.checked)selected.add(box.value);else selected.delete(box.value);});
   persist();
  });
 }
 const actionForm=document.querySelector("form[action='/github/exploration/repositories/action']");
 if(actionForm){
  actionForm.addEventListener('submit',()=>{
   actionForm.querySelectorAll('input[data-persisted-selection]').forEach(node=>node.remove());
   const visible=new Set(boxes.map(box=>box.value));
   selected.forEach(value=>{
    if(!visible.has(value)){
     const hidden=document.createElement('input');
     hidden.type='hidden';hidden.name='repositories';hidden.value=value;
     hidden.dataset.persistedSelection='1';actionForm.appendChild(hidden);
    }
   });
  });
 }
 persist();
 const statusUrl='/github/exploration/status';
 async function refresh(){
  try{
   const r=await fetch(statusUrl,{cache:'no-store'});
   if(!r.ok)throw new Error('HTTP '+r.status);
   const d=await r.json();
   const status=document.getElementById('job-status');
   const progress=document.getElementById('job-progress');
   const message=document.getElementById('job-message');
   if(status)status.textContent=d.job_status;
   if(progress)progress.textContent='Páginas de búsqueda: '+d.progress_discoveries+' · Repositorios inspeccionados: '+d.progress_inspections+' · Repositorios guardados: '+Object.values(d.repositories||{}).reduce((a,b)=>a+b,0);
   if(message)message.textContent=d.last_message||'Esperando actividad del explorador…';
   const button=document.querySelector("form[action='/github/exploration/run'] button");
   if(button)button.disabled=['queued','running'].includes(d.job_status);
   if(['queued','running'].includes(d.job_status))setTimeout(refresh,5000);
   else if(document.querySelector("tr[data-processing='running'],tr[data-processing='queued'],tr[data-processing='pending']"))setTimeout(()=>location.reload(),5000);
  }catch(e){
   const message=document.getElementById('job-message');
   if(message)message.textContent='No se pudo consultar el estado; se volverá a intentar automáticamente.';
   setTimeout(refresh,5000);
  }
 }
 // Consultar inmediatamente tras cargar la página, aunque el HTML inicial
 // muestre un estado antiguo. Después se mantiene el sondeo mientras trabaja.
 setTimeout(refresh,500);
})();
</script>"""
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem'>"+body+live_script+"</body></html>")

@router.post("/github/exploration/repositories/action")
def exploration_repository_action(
    action: str = Form(...),
    repositories: list[str] = Form(default=[]),
    return_page: int = Form(1),
    return_status: str = Form(""),
    return_q: str = Form(""),
):
    ensure_schema()
    selected = list(dict.fromkeys(
        value.strip() for value in repositories
        if value and "/" in value and len(value) <= 200
    ))
    if not selected or action not in {"ignore", "remove", "process", "restore", "catalog"}:
        return RedirectResponse("/github/exploration", status_code=303)
    if action == "catalog":
        from app import github_catalog, github_processing
        github_catalog.DB_PATH = DB_PATH
        github_processing.DB_PATH = DB_PATH
        github_catalog.ensure_schema()
        github_processing.ensure_processing_schema()
        with db() as source:
            rows = source.execute(
                "SELECT * FROM github_discovered_repositories WHERE full_name IN ("+
                ",".join("?" for _ in selected)+")", selected
            ).fetchall()
        eligible = [
            row for row in rows
            if row["last_checked_at"]
            and row["status"] in ("qualified", "partial")
            and int(row["musical_file_count"] or 0) >= MIN_MUSICAL_FILES
        ]
        added = 0
        processing_ids = []
        with github_catalog.db() as target:
            for row in eligible:
                target.execute("""
                    INSERT INTO github_repositories
                    (full_name,html_url,default_branch,description,license_spdx,license_name,stars,added_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(full_name) DO UPDATE SET
                      html_url=excluded.html_url,default_branch=excluded.default_branch,
                      description=excluded.description,license_spdx=excluded.license_spdx,
                      stars=excluded.stars
                """, (row["full_name"],row["html_url"],row["default_branch"],row["description"],
                      row["license_spdx"],row["license_spdx"],row["stars"],now()))
                repository = target.execute(
                    "SELECT id FROM github_repositories WHERE full_name=?",
                    (row["full_name"],),
                ).fetchone()
                if repository:
                    repository_id = int(repository["id"])
                    processing_ids.append(repository_id)
                    target.execute("""
                        INSERT INTO github_processing_jobs
                          (repository_id,status,stage,progress_count,total_count,current_path,
                           started_at,updated_at,error_text)
                        VALUES(?,'queued','queued',0,0,'',?,?,NULL)
                        ON CONFLICT(repository_id) DO UPDATE SET
                          status=CASE WHEN github_processing_jobs.status='completed'
                                      THEN 'queued' ELSE 'queued' END,
                          stage='queued',progress_count=0,total_count=0,current_path='',
                          updated_at=excluded.updated_at,error_text=NULL
                    """, (repository_id, now(), now()))
                added += 1
        # Incorporation and full processing are one operation.
        from app.github_processing import enqueue_repositories
        enqueue_repositories(processing_ids)
        skipped = len(rows) - len(eligible)
        return RedirectResponse(
            "/github/exploration?catalog_added="+str(added)+"&processing_started="+str(len(processing_ids))+"&catalog_skipped="+str(skipped),
            status_code=303,
        )
    placeholders = ",".join("?" for _ in selected)
    with db() as c:
        if action == "remove":
            c.execute(
                f"DELETE FROM github_discovered_repositories WHERE full_name IN ({placeholders})",
                selected,
            )
        elif action == "ignore":
            c.execute(
                f"UPDATE github_discovered_repositories SET status='ignored' "
                f"WHERE full_name IN ({placeholders})",
                selected,
            )
        elif action == "process":
            c.execute(
                f"""UPDATE github_discovered_repositories
                    SET status='pending',last_error=NULL,below_threshold=0
                    WHERE full_name IN ({placeholders}) AND status!='ignored'""",
                selected,
            )
        else:
            c.execute(
                f"""UPDATE github_discovered_repositories
                    SET status='pending',last_error=NULL,below_threshold=0
                    WHERE full_name IN ({placeholders}) AND status='ignored'""",
                selected,
            )
    destination = "/github/exploration?page="+str(max(1,return_page))
    if return_status: destination += "&status="+quote(return_status)
    if return_q: destination += "&q="+quote(return_q)
    return RedirectResponse(destination, status_code=303)


@router.post("/github/exploration/repository/incorporate")
def incorporate_discovered_repository(full_name: str = Form(...)):
    ensure_schema()
    from app import github_catalog, github_processing
    from app.github_processing import enqueue_repository
    github_catalog.DB_PATH = DB_PATH
    github_processing.DB_PATH = DB_PATH
    github_catalog.ensure_schema()
    github_processing.ensure_processing_schema()
    with db() as source:
        row=source.execute("""SELECT * FROM github_discovered_repositories
          WHERE full_name=? AND status IN ('qualified','partial') AND musical_file_count>=?""",
          (full_name,MIN_MUSICAL_FILES)).fetchone()
    if not row:
        return RedirectResponse("/github/exploration",status_code=303)
    with github_catalog.db() as target:
        target.execute("""INSERT INTO github_repositories
          (full_name,html_url,default_branch,description,license_spdx,license_name,stars,added_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(full_name) DO UPDATE SET
          html_url=excluded.html_url,default_branch=excluded.default_branch,
          description=excluded.description,license_spdx=excluded.license_spdx,stars=excluded.stars""",
          (row["full_name"],row["html_url"],row["default_branch"],row["description"],
           row["license_spdx"],row["license_spdx"],row["stars"],now()))
        repo=target.execute("SELECT id FROM github_repositories WHERE full_name=?",(full_name,)).fetchone()
    if repo:
        with github_catalog.db() as target:
            target.execute("""INSERT INTO github_processing_jobs
                (repository_id,status,stage,progress_count,total_count,started_at,updated_at,error_text)
                VALUES(?,'queued','queued',0,0,?, ?,NULL)
                ON CONFLICT(repository_id) DO UPDATE SET status='queued',stage='queued',
                progress_count=0,total_count=0,updated_at=excluded.updated_at,error_text=NULL""",
                (int(repo["id"]),now(),now()))
        enqueue_repository(int(repo["id"]))
    return RedirectResponse("/github/exploration",status_code=303)


_exploration_task: asyncio.Task | None = None


async def run_exploration_worker():
    ensure_schema()
    started = time.monotonic()
    discoveries = 0
    inspections = 0
    rate_limit = None
    with db() as c:
        state = c.execute(
            "SELECT requested_minutes,resume_after FROM github_exploration_state WHERE id=1"
        ).fetchone()
    budget = max(1, int(state["requested_minutes"] or 60)) * 60
    logging.getLogger(__name__).info(
        "Exploración GitHub iniciada: presupuesto máximo de %s minutos", int(budget / 60)
    )
    no_more_work = False
    if state["resume_after"]:
        try:
            resume_dt = datetime.fromisoformat(state["resume_after"])
            if datetime.now(timezone.utc) < resume_dt:
                message = f"GitHub ha pedido esperar hasta {state['resume_after']} (UTC)."
                with db() as c:
                    c.execute("""UPDATE github_exploration_state SET enabled=0,job_status='rate_limited',
                      last_run_at=?,last_run_seconds=0,last_message=? WHERE id=1""", (now(), message))
                logging.getLogger(__name__).warning("Exploración GitHub en espera: %s", message)
                return
        except ValueError:
            pass
    try:
        with db() as c:
            c.execute("""UPDATE github_exploration_state SET job_status='running',
              progress_discoveries=0,progress_inspections=0,last_message='Exploración en ejecución.' WHERE id=1""")
        async with httpx.AsyncClient(timeout=25, headers=headers(), follow_redirects=True) as client:
            while time.monotonic() - started < budget:
                did, msg = await discover_one(client)
                if did:
                    discoveries += 1
                    logging.getLogger(__name__).info("Exploración GitHub: %s", msg)
                with db() as c:
                    c.execute("""UPDATE github_exploration_state SET progress_discoveries=?,
                      progress_inspections=?,last_message=? WHERE id=1""",
                      (discoveries, inspections, msg if did else "Buscando repositorios pendientes."))
                if time.monotonic() - started >= budget:
                    break
                did, msg = await inspect_one(client)
                if did:
                    inspections += 1
                    logging.getLogger(__name__).info("Exploración GitHub: %s", msg)
                with db() as c:
                    c.execute("""UPDATE github_exploration_state SET progress_discoveries=?,
                      progress_inspections=?,last_message=? WHERE id=1""",
                      (discoveries, inspections, msg if did else "No hay más repositorios pendientes de inspección."))
                if not did and discoveries == 0 and inspections == 0:
                    no_more_work = True
                    break
                if not did and discoveries > 0:
                    with db() as c:
                        pending = c.execute("SELECT COUNT(*) n FROM github_discovered_repositories WHERE status='pending'").fetchone()["n"]
                        todo = c.execute("SELECT COUNT(*) n FROM github_discovery_queries WHERE status IN ('pending','running') AND page<=?", (MAX_SEARCH_PAGES,)).fetchone()["n"]
                    if not pending and not todo:
                        no_more_work = True
                        break
    except GitHubRateLimit as exc:
        rate_limit = exc
    except asyncio.CancelledError:
        elapsed = round(time.monotonic() - started, 1)
        with db() as c:
            c.execute("""UPDATE github_exploration_state SET enabled=1,job_status='paused',
              last_run_at=?,last_run_seconds=?,last_message='Exploración pausada por reinicio. Se reanudará al arrancar la aplicación.'
              WHERE id=1""", (now(), elapsed))
        raise
    except Exception as exc:
        logging.getLogger(__name__).exception("Error en el trabajador de exploración GitHub")
        elapsed = round(time.monotonic() - started, 1)
        with db() as c:
            c.execute("""UPDATE github_exploration_state SET enabled=0,job_status='error',
              last_run_at=?,last_run_seconds=?,last_message=? WHERE id=1""",
              (now(), elapsed, f"Error en exploración: {type(exc).__name__}: {exc}"[:1000]))
        return
    elapsed = round(time.monotonic() - started, 1)
    if rate_limit:
        message = f"Exploración pausada por límite de GitHub. {discoveries} páginas y {inspections} repositorios procesados en {elapsed} s. {rate_limit}"
        status = "rate_limited"
    else:
        if no_more_work:
            message = (
                f"No quedan consultas ni repositorios pendientes. "
                f"Se inspeccionaron {discoveries} páginas y {inspections} repositorios en {elapsed} s; "
                f"el presupuesto era de {int(budget / 60)} minutos."
            )
        else:
            message = (
                f"Presupuesto de {int(budget / 60)} minutos agotado: "
                f"{discoveries} páginas de descubrimiento y {inspections} repositorios inspeccionados."
            )
        status = "completed"
    with db() as c:
        c.execute("""UPDATE github_exploration_state SET enabled=0,job_status=?,
          last_run_at=?,last_run_seconds=?,last_message=?,resume_after=? WHERE id=1""",
          (status, now(), elapsed, message, rate_limit.resume_after if rate_limit else None))
    logging.getLogger(__name__).info("Exploración GitHub finalizada: %s", message)


def start_exploration_worker():
    global _exploration_task
    if _exploration_task is not None and not _exploration_task.done():
        return _exploration_task
    _exploration_task = asyncio.create_task(run_exploration_worker(), name="github-exploration")
    return _exploration_task


async def stop_exploration_worker():
    global _exploration_task
    if _exploration_task is not None and not _exploration_task.done():
        _exploration_task.cancel()
        await asyncio.gather(_exploration_task, return_exceptions=True)
    _exploration_task = None


def recover_exploration_state():
    """Do not launch a long GitHub crawl implicitly when the app starts."""
    ensure_schema()
    with db() as c:
        c.execute("""UPDATE github_exploration_state
          SET enabled=0,
              job_status=CASE WHEN job_status IN ('queued','running') THEN 'paused' ELSE job_status END,
              last_message=CASE WHEN job_status IN ('queued','running')
                THEN 'Exploración pausada al iniciar la aplicación. Pulsa Continuar y fija el tiempo para reanudar.'
                ELSE last_message END
          WHERE id=1""")
    return None


@router.get("/github/exploration/status")
def exploration_status():
    ensure_schema()
    with db() as c:
        state = c.execute("""SELECT job_status,progress_discoveries,progress_inspections,
          last_message,last_run_at,last_run_seconds,resume_after FROM github_exploration_state WHERE id=1""").fetchone()
        counts = c.execute("""SELECT status,COUNT(*) n FROM github_discovered_repositories GROUP BY status""").fetchall()
    return {
        "job_status": state["job_status"],
        "progress_discoveries": state["progress_discoveries"],
        "progress_inspections": state["progress_inspections"],
        "last_message": state["last_message"],
        "last_run_at": state["last_run_at"],
        "last_run_seconds": state["last_run_seconds"],
        "resume_after": state["resume_after"],
        "repositories": {r["status"]: r["n"] for r in counts},
    }


@router.post("/github/exploration/run")
async def exploration_run(minutes: int = Form(60)):
    ensure_schema()
    global _exploration_task
    if _exploration_task is not None and not _exploration_task.done():
        return RedirectResponse("/github/exploration", status_code=303)
    with db() as c:
        state = c.execute("SELECT job_status,enabled,resume_after FROM github_exploration_state WHERE id=1").fetchone()
        if state["job_status"] == "running" and state["enabled"]:
            return RedirectResponse("/github/exploration", status_code=303)
        if state["resume_after"]:
            try:
                if datetime.now(timezone.utc) < datetime.fromisoformat(state["resume_after"]):
                    return RedirectResponse("/github/exploration", status_code=303)
            except ValueError:
                pass
        c.execute("""UPDATE github_exploration_state SET enabled=1,job_status='queued',
          requested_minutes=?,progress_discoveries=0,progress_inspections=0,
          last_message='Exploración en cola.' WHERE id=1""", (max(1, min(int(minutes), 1440)),))
    start_exploration_worker()
    return RedirectResponse("/github/exploration", status_code=303)
