from __future__ import annotations

import asyncio
import html
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import connect

router = APIRouter()
DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
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
          WHERE status IN ('pending','error') ORDER BY
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

@router.get("/github/exploration",response_class=HTMLResponse)
def exploration_home():
    ensure_schema()
    with db() as c:
        counts={r["status"]:r["n"] for r in c.execute(
          "SELECT status,COUNT(*) n FROM github_discovered_repositories GROUP BY status")}
        q=c.execute("SELECT COUNT(*) n FROM github_discovery_queries WHERE status!='done'").fetchone()["n"]
        state=c.execute("SELECT * FROM github_exploration_state WHERE id=1").fetchone()
        qualified=c.execute("""SELECT full_name,musical_file_count,updated_at,last_checked_at
          FROM github_discovered_repositories WHERE status='qualified'
          ORDER BY musical_file_count DESC LIMIT 30""").fetchall()
    total=sum(counts.values())
    body="<h1>Exploración persistente de GitHub</h1>"
    body+="<p>La exploración se conserva en SQLite. Cada ejecución continúa desde la siguiente página o repositorio pendiente; no vuelve a empezar desde el principio.</p>"
    body+="<section class='panel'><h2>Presupuesto de esta ejecución</h2><form method='post' action='/github/exploration/run'>"
    body+="<label>Tiempo máximo (minutos) <input type='number' name='minutes' min='1' max='20' value='3'></label> "
    body+="<button>Continuar exploración</button></form>"
    body+="<p class='muted'>Se alterna descubrimiento por páginas y comprobación de repositorios. El proceso se detiene al alcanzar el presupuesto o agotar las tareas disponibles. Puedes volver a ejecutarlo cuando quieras.</p></section>"
    body+="<section class='panel'><h2>Estado acumulado</h2><p>Repositorios guardados: <b>"+str(total)+"</b> · Páginas/consultas pendientes: <b>"+str(q)+"</b></p><ul>"
    for label in ("pending","qualified","below_threshold","partial","error"):
        body+="<li>"+label+": "+str(counts.get(label,0))+"</li>"
    body+="</ul><p>Última ejecución: "+esc(state["last_run_at"] or "ninguna")+" · "+esc(state["last_message"])+"</p>"
    if state["resume_after"]:
        body+="<p><b>Reanudación disponible a partir de:</b> "+esc(state["resume_after"])+" (UTC)</p>"
    body+="</section>"
    body+="<section class='panel'><h2>Repositorios que cumplen el mínimo de 11 archivos</h2>"
    if qualified:
        body+="<table><tr><th>Repositorio</th><th>Archivos musicales</th><th>Actualización</th><th>Comprobado</th></tr>"
        for r in qualified:
            body+="<tr><td><a href='https://github.com/"+esc(r["full_name"])+"'>"+esc(r["full_name"])+"</a></td><td>"+str(r["musical_file_count"])+"</td><td>"+esc(r["updated_at"][:10])+"</td><td>"+esc(r["last_checked_at"])+"</td></tr>"
        body+="</table>"
    else: body+="<p>Aún no hay repositorios que cumplan el criterio.</p>"
    body+="</section><p><a href='/github'>Volver a Fuentes GitHub</a></p>"
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem'>"+body+"</body></html>")

@router.post("/github/exploration/run")
async def exploration_run(minutes:int=Form(3)):
    ensure_schema()
    budget=max(1,min(20,int(minutes)))*60
    started=time.monotonic(); messages=[]; discoveries=0; inspections=0
    rate_limit = None
    with db() as c:
        state = c.execute("SELECT resume_after FROM github_exploration_state WHERE id=1").fetchone()
    if state and state["resume_after"]:
        try:
            resume_dt = datetime.fromisoformat(state["resume_after"])
            if datetime.now(timezone.utc) < resume_dt:
                message = f"GitHub ha pedido esperar hasta {state['resume_after']} (UTC). No se ha iniciado otra petición."
                with db() as c:
                    c.execute("UPDATE github_exploration_state SET last_run_at=?,last_run_seconds=0,last_message=? WHERE id=1",
                              (now(),message))
                return RedirectResponse("/github/exploration",status_code=303)
        except ValueError:
            pass
    try:
        async with httpx.AsyncClient(timeout=25,headers=headers(),follow_redirects=True) as client:
            while time.monotonic()-started < budget:
                did,msg=await discover_one(client)
                if did:
                    discoveries+=1; messages.append(msg)
                if time.monotonic()-started >= budget: break
                did,msg=await inspect_one(client)
                if did:
                    inspections+=1; messages.append(msg)
                if not did and discoveries==0 and inspections==0: break
                if not did and discoveries>0:
                    with db() as c:
                        pending=c.execute("SELECT COUNT(*) n FROM github_discovered_repositories WHERE status IN ('pending','error')").fetchone()["n"]
                        todo=c.execute("SELECT COUNT(*) n FROM github_discovery_queries WHERE status IN ('pending','running') AND page<=?",(MAX_SEARCH_PAGES,)).fetchone()["n"]
                    if not pending and not todo: break
    except GitHubRateLimit as exc:
        rate_limit = exc
    elapsed=round(time.monotonic()-started,1)
    if rate_limit:
        message=f"Exploración pausada por límite de GitHub. {discoveries} páginas y {inspections} repositorios procesados en {elapsed} s. {rate_limit}"
    else:
        message=f"{discoveries} páginas de descubrimiento y {inspections} repositorios inspeccionados en {elapsed} s."
    with db() as c:
        c.execute("UPDATE github_exploration_state SET last_run_at=?,last_run_seconds=?,last_message=?,resume_after=? WHERE id=1",
                  (now(),elapsed,message,rate_limit.resume_after if rate_limit else None))
    return RedirectResponse("/github/exploration",status_code=303)
