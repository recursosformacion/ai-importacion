from __future__ import annotations

import asyncio
import html
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

@router.get("/github/exploration",response_class=HTMLResponse)
def exploration_home(page: int = 1, status: str = "", q: str = ""):
    ensure_schema()
    page = max(1, int(page or 1))
    status = status if status in {"pending","qualified","below_threshold","partial","error","ignored"} else ""
    q = q.strip()[:120]
    page_size = 100
    where = []
    params = []
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
        repositories=c.execute("""SELECT full_name,description,status,musical_file_count,
          updated_at,last_checked_at,last_error FROM github_discovered_repositories"""+clause+"""
          ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'error' THEN 1
            WHEN 'qualified' THEN 2 WHEN 'below_threshold' THEN 3
            WHEN 'partial' THEN 4 WHEN 'ignored' THEN 5 ELSE 6 END,
            discovered_at DESC LIMIT ? OFFSET ?""",params+[page_size,(page-1)*page_size]).fetchall()
    total=sum(counts.values())
    body="<h1>Exploración persistente de GitHub</h1>"
    body+="<p>La exploración se conserva en SQLite. Cada ejecución continúa desde la siguiente página o repositorio pendiente; no vuelve a empezar desde el principio.</p>"
    body+="<p>Autenticación GitHub: <b>"+("configurada" if TOKEN else "sin token")+"</b>.</p>"
    body+="<section class='panel'><h2>Presupuesto de esta ejecución</h2><form method='post' action='/github/exploration/run'>"
    body+="<label>Tiempo máximo (minutos) <input type='number' name='minutes' min='1' value='60'></label> "
    body+="<button>Continuar exploración</button></form>"
    body+="<p class='muted'>Se alterna descubrimiento por páginas y comprobación de repositorios. El proceso se detiene al alcanzar el presupuesto o agotar las tareas disponibles. Puedes volver a ejecutarlo cuando quieras.</p></section>"
    body+="<section class='panel'><h2>Estado acumulado</h2><p>Repositorios guardados: <b>"+str(total)+"</b> · Páginas/consultas pendientes: <b>"+str(q_pending)+"</b></p><ul>"
    for label in ("pending","qualified","below_threshold","partial","error","ignored"):
        body+="<li>"+label+": "+str(counts.get(label,0))+"</li>"
    body+="</ul><p>Última ejecución: "+esc(state["last_run_at"] or "ninguna")+" · "+esc(state["last_message"])+"</p>"
    if state["resume_after"]:
        body+="<p><b>Reanudación disponible a partir de:</b> "+esc(state["resume_after"])+" (UTC)</p>"
    body+="</section>"
    body+="<section class='panel'><h2>Repositorios descubiertos</h2>"
    body+="<p>Se muestran "+str(len(repositories))+" de "+str(filtered_total)+" resultados filtrados. La paginación permite recorrer todo el catálogo.</p>"
    body+="<form method='get' action='/github/exploration'><label>Buscar <input name='q' value='"+esc(q)+"' placeholder='Propietario, repositorio o descripción'></label> "
    body+="<label>Estado <select name='status'><option value=''>Todos</option>"
    for value,label in (("pending","Pendiente"),("qualified","Cumple mínimo"),("below_threshold","Por debajo del mínimo"),("partial","Árbol parcial"),("error","Error"),("ignored","Ignorado")):
        body+="<option value='"+value+"'"+(" selected" if status==value else "")+">"+label+"</option>"
    body+="</select></label> <button>Filtrar</button> <a href='/github/exploration'>Limpiar</a></form>"
    if repositories:
        body+="<form method='post' action='/github/exploration/repositories/action'>"
        body+="<input type='hidden' name='return_page' value='"+str(page)+"'><input type='hidden' name='return_status' value='"+esc(status)+"'><input type='hidden' name='return_q' value='"+esc(q)+"'>"
        body+="<p><label><input type='checkbox' onclick='document.querySelectorAll(\"input.repo-select\").forEach(x=>x.checked=this.checked)'> Seleccionar todos los visibles</label> "
        body+="<button name='action' value='ignore'>Ignorar</button> "
        body+="<button name='action' value='process'>Volver a inspeccionar</button> "
        body+="<button name='action' value='restore'>Restaurar</button> "
        body+="<button name='action' value='catalog'>Incorporar al catálogo</button></p>"
        body+="<table><tr><th>Sel.</th><th>Repositorio</th><th>Estado</th><th>Archivos musicales</th><th>Actualizado</th><th>Comprobado</th></tr>"
        for r in repositories:
            status_label={"pending":"Pendiente","qualified":"Cumple mínimo","below_threshold":"Por debajo del mínimo","partial":"Árbol parcial","error":"Error","ignored":"Ignorado"}.get(r["status"],r["status"])
            detail=("<div class='muted'>"+esc(r["last_error"])+"</div>") if r["last_error"] else ""
            body+="<tr><td><input class='repo-select' type='checkbox' name='repositories' value='"+esc(r["full_name"])+"'></td>"
            body+="<td><a href='https://github.com/"+esc(r["full_name"])+"'>"+esc(r["full_name"])+"</a><div class='muted'>"+esc(r["description"])+"</div>"+detail+"</td>"
            body+="<td>"+esc(status_label)+"</td><td>"+str(r["musical_file_count"])+"</td>"
            body+="<td>"+esc((r["updated_at"] or "")[:10])+"</td><td>"+esc(r["last_checked_at"] or "—")+"</td></tr>"
        body+="</table></form>"
        pages=max(1,(filtered_total+page_size-1)//page_size)
        body+="<p class='pagination'>"
        if page>1: body+="<a href='/github/exploration?page="+str(page-1)+"&status="+esc(status)+"&q="+quote(q)+"'>← Anterior</a> "
        body+="Página "+str(page)+" de "+str(pages)
        if page<pages: body+=" <a href='/github/exploration?page="+str(page+1)+"&status="+esc(status)+"&q="+quote(q)+"'>Siguiente →</a>"
        body+="</p>"
    else:
        body+="<p>No hay repositorios para estos filtros.</p>"
    body+="</section><p><a href='/github'>Volver a Fuentes GitHub</a></p>"
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem'>"+body+"</body></html>")

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
    if not selected or action not in {"ignore", "process", "restore", "catalog"}:
        return RedirectResponse("/github/exploration", status_code=303)
    if action == "catalog":
        from app import github_catalog
        github_catalog.ensure_schema()
        with db() as source:
            rows = source.execute(
                "SELECT * FROM github_discovered_repositories WHERE full_name IN ("+
                ",".join("?" for _ in selected)+")", selected
            ).fetchall()
        added = 0
        with github_catalog.db() as target:
            for row in rows:
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
                added += 1
        return RedirectResponse("/github?imported="+str(added), status_code=303)
    placeholders = ",".join("?" for _ in selected)
    with db() as c:
        if action == "ignore":
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
    if state["resume_after"]:
        try:
            resume_dt = datetime.fromisoformat(state["resume_after"])
            if datetime.now(timezone.utc) < resume_dt:
                message = f"GitHub ha pedido esperar hasta {state['resume_after']} (UTC)."
                with db() as c:
                    c.execute("""UPDATE github_exploration_state SET enabled=0,job_status='rate_limited',
                      last_run_at=?,last_run_seconds=0,last_message=? WHERE id=1""", (now(), message))
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
                with db() as c:
                    c.execute("""UPDATE github_exploration_state SET progress_discoveries=?,
                      progress_inspections=?,last_message=? WHERE id=1""",
                      (discoveries, inspections, msg if did else "Buscando repositorios pendientes."))
                if time.monotonic() - started >= budget:
                    break
                did, msg = await inspect_one(client)
                if did:
                    inspections += 1
                with db() as c:
                    c.execute("""UPDATE github_exploration_state SET progress_discoveries=?,
                      progress_inspections=?,last_message=? WHERE id=1""",
                      (discoveries, inspections, msg if did else "No hay más repositorios pendientes de inspección."))
                if not did and discoveries == 0 and inspections == 0:
                    break
                if not did and discoveries > 0:
                    with db() as c:
                        pending = c.execute("SELECT COUNT(*) n FROM github_discovered_repositories WHERE status='pending'").fetchone()["n"]
                        todo = c.execute("SELECT COUNT(*) n FROM github_discovery_queries WHERE status IN ('pending','running') AND page<=?", (MAX_SEARCH_PAGES,)).fetchone()["n"]
                    if not pending and not todo:
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
        message = f"{discoveries} páginas de descubrimiento y {inspections} repositorios inspeccionados en {elapsed} s."
        status = "completed"
    with db() as c:
        c.execute("""UPDATE github_exploration_state SET enabled=0,job_status=?,
          last_run_at=?,last_run_seconds=?,last_message=?,resume_after=? WHERE id=1""",
          (status, now(), elapsed, message, rate_limit.resume_after if rate_limit else None))


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


def resume_exploration_worker():
    ensure_schema()
    with db() as c:
        state = c.execute("SELECT enabled,job_status FROM github_exploration_state WHERE id=1").fetchone()
    if state and state["enabled"]:
        return start_exploration_worker()
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
                        pending=c.execute("SELECT COUNT(*) n FROM github_discovered_repositories WHERE status='pending'").fetchone()["n"]
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
