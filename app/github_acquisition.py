from __future__ import annotations

import base64
import html
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import connect

router = APIRouter()
DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
API = "https://api.github.com"
try:
    from app.github_exploration import TOKEN
except ImportError:
    TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
USER_AGENT = "OSAP-AI-Importacion/0.1 (+https://openmusicrepository.com)"
MAX_README_CHARS = 200_000
MAX_TREE_ENTRIES = 50_000

REFERENCE_TERMS = (
    "cite", "citation", "credit", "acknowledg", "attribution",
    "public use", "please mention", "please acknowledge", "mention",
    "mencionar", "mencion", "mención", "uso público", "enlace", "atribución", "crédito", "agradecimiento",
)
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)(?:\s+[^)]*)?\)", re.I)
URL_RE = re.compile(r"https?://[^\s)>]+", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value: object | None) -> str:
    return html.escape("" if value is None else str(value))


def db():
    return connect(DB_PATH)


def ensure_schema() -> None:
    from app.github_catalog import ensure_schema as ensure_github_schema
    ensure_github_schema()
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS github_repository_acquisition (
          repository_id INTEGER PRIMARY KEY REFERENCES github_repositories(id) ON DELETE CASCADE,
          readme_path TEXT NOT NULL DEFAULT '',
          readme_sha TEXT NOT NULL DEFAULT '',
          readme_content TEXT NOT NULL DEFAULT '',
          tree_json TEXT NOT NULL DEFAULT '[]',
          strategy_type TEXT NOT NULL DEFAULT 'github_tree',
          plan_json TEXT NOT NULL DEFAULT '{}',
          inspection_status TEXT NOT NULL DEFAULT 'pending',
          inspection_error TEXT,
          inspected_at TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS github_repository_references (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repository_id INTEGER NOT NULL REFERENCES github_repositories(id) ON DELETE CASCADE,
          reference_type TEXT NOT NULL DEFAULT 'attribution',
          label TEXT NOT NULL,
          url TEXT NOT NULL DEFAULT '',
          instruction_text TEXT NOT NULL DEFAULT '',
          source_path TEXT NOT NULL DEFAULT '',
          source_excerpt TEXT NOT NULL DEFAULT '',
          required INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(repository_id,reference_type,label,url,source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_github_repo_refs
          ON github_repository_references(repository_id,reference_type);
        """)


def headers() -> dict[str, str]:
    result = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        result["Authorization"] = "Bearer " + TOKEN
    return result


async def github_get(url: str) -> dict:
    async with httpx.AsyncClient(timeout=45, headers=headers()) as client:
        response = await client.get(url)
        if response.status_code == 404:
            raise ValueError("GitHub no encuentra el README o el árbol del repositorio.")
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            raise ValueError("Se ha alcanzado el límite de consultas de GitHub.")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("GitHub devolvió una respuesta inesperada.")
    return payload


def markdown_paragraphs(markdown: str):
    return [part.strip() for part in re.split(r"\n\s*\n", markdown) if part.strip()]


def extract_reference_candidates(markdown: str, readme_path: str) -> list[dict]:
    found: list[dict] = []
    for paragraph in markdown_paragraphs(markdown):
        normalized = re.sub(r"\s+", " ", paragraph).strip()
        lowered = normalized.casefold()
        if not any(term in lowered for term in REFERENCE_TERMS):
            continue
        links = LINK_RE.findall(paragraph)
        project_match = re.search(
            r"(?:mention|mencion\w*|credit|cite|acknowledg\w*)\s+(?:a\s+)?([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ-]*(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ-]*){1,5})",
            paragraph,
        )
        if links:
            for label, url in links:
                found.append({
                    "reference_type": "attribution",
                    "label": project_match.group(1).strip() if project_match else (re.sub(r"[*_]", "", label).strip() or "Referencia indicada en README"),
                    "url": url.rstrip(".,;"),
                    "instruction_text": re.sub(r"[*_]", "", normalized),
                    "source_path": readme_path,
                    "source_excerpt": normalized[:4000],
                    "required": 1,
                })
        else:
            urls = URL_RE.findall(paragraph)
            found.append({
                "reference_type": "attribution",
                "label": "Referencia indicada en README",
                "url": urls[0].rstrip(".,;") if urls else "",
                "instruction_text": re.sub(r"[*_]", "", normalized),
                "source_path": readme_path,
                "source_excerpt": normalized[:4000],
                "required": 1,
            })
    return found



def build_acquisition_plan(readme: str, tree: list[dict]) -> dict:
    """Infer a reviewable acquisition strategy; never execute README commands."""
    paths = [
        str(item.get("path") or "")
        for item in tree
        if item.get("type") == "blob" and item.get("path")
    ]
    lowered = [path.casefold() for path in paths]
    readme_lower = readme.casefold()
    manifest_names = (
        "manifest", "corpus_conversion", "scores.tsv", "scores.yaml",
        "scores.yml", "files.tsv", "files.csv", "inventory", "catalog",
        "catalogue", "index.json",
    )
    manifests = [
        path for path in paths
        if any(name in path.casefold().rsplit("/", 1)[-1] for name in manifest_names)
        and path.casefold().endswith((".json", ".csv", ".tsv", ".yaml", ".yml", ".xml"))
    ]
    score_dirs = sorted({
        path.split("/", 1)[0] if "/" in path else ""
        for path in paths
        if any(token in path.casefold().split("/") for token in ("scores", "score", "music", "parts", "partitions"))
    })
    command_blocks = re.findall(r"```(?:[a-zA-Z0-9_-]+)?\s*\n(.*?)\n```", readme, re.S)
    documented_commands = [
        block.strip()[:2000] for block in command_blocks
        if re.search(r"mscore|musescore|convert|batch|corpus_conversion|download", block, re.I)
    ][:10]
    if manifests and any(term in readme_lower for term in ("manifest", "corpus_conversion", "batch convert")):
        strategy = "manifest"
        rationale = "La documentación y el árbol apuntan a uno o varios manifiestos que describen los ficheros o sus conversiones."
    elif score_dirs:
        strategy = "directory_rules"
        rationale = "El árbol organiza partituras en carpetas reconocibles; conviene seleccionar por reglas de directorio y preservar rutas."
    else:
        strategy = "github_tree"
        rationale = "No se ha identificado todavía un manifiesto o una estructura musical suficientemente explícita; se propone adquirir desde el árbol GitHub."
    extension_counts = {}
    for path in paths:
        extension = os.path.splitext(path)[1].lower() or "(sin extensión)"
        extension_counts[extension] = extension_counts.get(extension, 0) + 1
    return {
        "strategy_type": strategy,
        "rationale": rationale,
        "manifest_paths": manifests[:100],
        "score_directories": [value for value in score_dirs if value][:100],
        "documented_commands": documented_commands,
        "file_count": len(paths),
        "extension_counts": dict(sorted(extension_counts.items())),
        "requires_review": True,
    }

def page(title: str, body: str) -> HTMLResponse:
    style = """
    <style>
    body{font:16px system-ui,sans-serif;color:#20242a;background:#f4f6f8;margin:0}
    header{background:#15283f;color:white;padding:1rem max(1rem,calc((100% - 1100px)/2))}
    header a{color:white;margin-right:1.2rem;text-decoration:none}
    main{max-width:1100px;margin:1.5rem auto;padding:0 1rem}
    .panel{background:white;border:1px solid #dce2e8;border-radius:10px;padding:1rem;margin:1rem 0}
    input,select,textarea,button{font:inherit;padding:.65rem;border:1px solid #aab6c4;border-radius:6px;max-width:100%}
    input[type=text],textarea{width:min(100%,55rem)} textarea{min-height:6rem}
    button{background:#174f89;color:white;border:0;cursor:pointer}
    table{border-collapse:collapse;width:100%;background:white}
    td,th{text-align:left;padding:.65rem;border-bottom:1px solid #e1e6eb;vertical-align:top}
    th{background:#edf1f5}.muted{color:#5d6977;font-size:.9rem}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f4f6;padding:1rem;max-height:32rem;overflow:auto}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1rem}
    </style>"""
    return HTMLResponse(
        "<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>" + esc(title) + "</title>" + style
        + "<header><b><a href='/'>AI Importación</a></b><a href='/github'>Fuentes GitHub</a></header>"
        + "<main>" + body + "<p class='muted'>La documentación y las referencias se conservan como procedencia. La disponibilidad de un archivo no confirma por sí sola que su reutilización esté permitida.</p></main></html>"
    )


@router.get("/github/{repository_id}/acquisition", response_class=HTMLResponse)
def acquisition_page(repository_id: int):
    ensure_schema()
    with db() as conn:
        repo = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not repo:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        acquisition = conn.execute(
            "SELECT * FROM github_repository_acquisition WHERE repository_id=?", (repository_id,)
        ).fetchone()
        references = conn.execute(
            "SELECT * FROM github_repository_references WHERE repository_id=? ORDER BY required DESC,id",
            (repository_id,),
        ).fetchall()
    body = "<h1>Adquisición: " + esc(repo["full_name"]) + "</h1>"
    body += "<p><a href='/github/" + str(repository_id) + "'>Volver al catálogo de archivos</a> · "
    body += "<a href='" + esc(repo["html_url"]) + "'>Abrir repositorio original</a></p>"
    body += "<section class='panel'><h2>1. Documentación y estructura</h2>"
    body += "<p>Lee el README y el árbol del repositorio antes de decidir qué descargar. Esta inspección no ejecuta scripts ni comandos del repositorio.</p>"
    body += "<form method='post' action='/github/" + str(repository_id) + "/acquisition/inspect'><button>Leer README y examinar estructura</button></form>"
    if acquisition:
        body += "<p class='muted'>Estado: " + esc(acquisition["inspection_status"]) + " · README: " + esc(acquisition["readme_path"] or "No encontrado") + " · Inspeccionado: " + esc(acquisition["inspected_at"] or "—") + "</p>"
        if acquisition["inspection_error"]:
            body += "<p>" + esc(acquisition["inspection_error"]) + "</p>"
        try:
            tree = json.loads(acquisition["tree_json"] or "[]")
        except json.JSONDecodeError:
            tree = []
        folders = {}
        for item in tree:
            if item.get("type") == "blob":
                folder = item.get("path", "").rsplit("/", 1)[0] or "(raíz)"
                folders[folder] = folders.get(folder, 0) + 1
        if folders:
            body += "<h3>Estructura detectada</h3><div class='grid'>"
            for folder, count in sorted(folders.items()):
                body += "<div><code>" + esc(folder) + "</code><div class='muted'>" + str(count) + " archivos</div></div>"
            body += "</div>"
        try:
            plan = json.loads(acquisition["plan_json"] or "{}")
        except json.JSONDecodeError:
            plan = {}
        if plan:
            body += "<h3>Plan preliminar · " + esc(plan.get("strategy_type", acquisition["strategy_type"])) + "</h3>"
            body += "<p>" + esc(plan.get("rationale", "")) + "</p>"
            body += "<p class='muted'>" + str(plan.get("file_count", 0)) + " archivos en el árbol · revisión humana obligatoria</p>"
            if plan.get("manifest_paths"):
                body += "<p><b>Manifiestos candidatos:</b></p><ul>" + "".join("<li><code>" + esc(path) + "</code></li>" for path in plan["manifest_paths"]) + "</ul>"
            if plan.get("score_directories"):
                body += "<p><b>Carpetas de partituras:</b> " + esc(", ".join(plan["score_directories"])) + "</p>"
            if plan.get("documented_commands"):
                body += "<details><summary>Comandos documentados (sólo informativos; no ejecutados)</summary>"
                for command in plan["documented_commands"]:
                    body += "<pre>" + esc(command) + "</pre>"
                body += "</details>"
        if acquisition["readme_content"]:
            body += "<details><summary>Ver README conservado</summary><pre>" + esc(acquisition["readme_content"]) + "</pre></details>"
    body += "</section>"
    body += "<section class='panel'><h2>2. Referencias y atribución</h2>"
    body += "<p>Las referencias detectadas en la documentación se guardan junto con el texto de atribución y la ubicación donde se encontró. Puedes corregirlas o añadir otras manualmente.</p>"
    if references:
        body += "<table><tr><th>Tipo</th><th>Referencia</th><th>URL</th><th>Texto que debe conservarse</th><th>Origen</th></tr>"
        for ref in references:
            body += "<tr><td>" + esc(ref["reference_type"]) + (" · requerida" if ref["required"] else "") + "</td>"
            body += "<td>" + esc(ref["label"]) + "</td><td>" + ("<a href='" + esc(ref["url"]) + "'>" + esc(ref["url"]) + "</a>" if ref["url"] else "—") + "</td>"
            body += "<td>" + esc(ref["instruction_text"] or ref["source_excerpt"]) + "</td><td>" + esc(ref["source_path"]) + "</td></tr>"
        body += "</table>"
    else:
        body += "<p class='muted'>No hay referencias registradas todavía. La ausencia de una detección automática no significa que el repositorio no exija atribución.</p>"
    body += "<h3>Añadir o registrar una referencia</h3>"
    body += "<form method='post' action='/github/" + str(repository_id) + "/acquisition/references'>"
    body += "<div class='grid'><label>Tipo<br><select name='reference_type'><option value='attribution'>Atribución</option><option value='project'>Proyecto o colección</option><option value='source'>Fuente original</option><option value='license'>Licencia</option></select></label>"
    body += "<label>Nombre o entidad<br><input name='label' type='text' required placeholder='OpenScore String Quartets'></label>"
    body += "<label>URL pública<br><input name='url' type='text' placeholder='https://...'></label></div>"
    body += "<p>Texto de atribución o condición de uso<br><textarea name='instruction_text' placeholder='Texto solicitado por el proyecto...'></textarea></p>"
    body += "<div class='grid'><label>Documento de origen<br><input name='source_path' type='text' placeholder='README.md'></label>"
    body += "<label><input type='checkbox' name='required' value='1' checked> Debe conservarse como requisito</label></div>"
    body += "<p><button>Guardar referencia</button></p></form></section>"
    body += "<section class='panel'><h2>3. Estrategia de adquisición</h2>"
    body += "<p>La estrategia se asignará después de revisar manifiestos, carpetas y formatos. El README se trata como documentación: sus comandos se interpretan, nunca se ejecutan automáticamente.</p>"
    body += "<p class='muted'>Estrategias previstas: árbol GitHub, manifiesto, reglas de directorio, activos de releases, clonación controlada o adaptador específico.</p></section>"
    return page("Adquisición " + repo["full_name"], body)


@router.post("/github/{repository_id}/acquisition/inspect")
async def inspect_repository(repository_id: int):
    ensure_schema()
    with db() as conn:
        repo = conn.execute("SELECT * FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
    if not repo:
        raise HTTPException(status_code=404, detail="Repositorio no encontrado")
    try:
        full_name = repo["full_name"]
        readme = await github_get(f"{API}/repos/{full_name}/readme")
        readme_path = str(readme.get("path") or "README.md")
        encoded = readme.get("content") or ""
        readme_content = base64.b64decode(encoded).decode("utf-8", errors="replace") if readme.get("encoding") == "base64" else str(encoded)
        readme_content = readme_content[:MAX_README_CHARS]
        metadata = await github_get(f"{API}/repos/{full_name}")
        branch = str(metadata.get("default_branch") or repo["default_branch"] or "main")
        tree_payload = await github_get(f"{API}/repos/{full_name}/git/trees/{quote(branch, safe='')}?recursive=1")
        tree = tree_payload.get("tree") or []
        if not isinstance(tree, list):
            raise ValueError("El árbol GitHub no tiene el formato esperado.")
        tree = tree[:MAX_TREE_ENTRIES]
        references = extract_reference_candidates(readme_content, readme_path)
        plan = build_acquisition_plan(readme_content, tree)
        stamp = now()
        with db() as conn:
            conn.execute("""
                INSERT INTO github_repository_acquisition
                (repository_id,readme_path,readme_sha,readme_content,tree_json,strategy_type,plan_json,inspection_status,inspection_error,inspected_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'completed',NULL,?,?)
                ON CONFLICT(repository_id) DO UPDATE SET
                  readme_path=excluded.readme_path,readme_sha=excluded.readme_sha,
                  readme_content=excluded.readme_content,tree_json=excluded.tree_json,
                  strategy_type=excluded.strategy_type,plan_json=excluded.plan_json,
                  inspection_status='completed',inspection_error=NULL,
                  inspected_at=excluded.inspected_at,updated_at=excluded.updated_at
            """, (repository_id, readme_path, str(readme.get("sha") or ""),
                  readme_content, json.dumps(tree, ensure_ascii=False), plan["strategy_type"],
                  json.dumps(plan, ensure_ascii=False), stamp, stamp))
            for ref in references:
                conn.execute("""
                    INSERT INTO github_repository_references
                    (repository_id,reference_type,label,url,instruction_text,source_path,source_excerpt,required,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(repository_id,reference_type,label,url,source_path) DO UPDATE SET
                      instruction_text=excluded.instruction_text,source_excerpt=excluded.source_excerpt,
                      required=excluded.required,updated_at=excluded.updated_at
                """, (repository_id, ref["reference_type"], ref["label"], ref["url"],
                      ref["instruction_text"], ref["source_path"], ref["source_excerpt"],
                      ref["required"], stamp, stamp))
            conn.execute("UPDATE github_repositories SET default_branch=? WHERE id=?", (branch, repository_id))
        return RedirectResponse(f"/github/{repository_id}/acquisition?inspection=completed", status_code=303)
    except (ValueError, httpx.HTTPError, UnicodeError) as exc:
        with db() as conn:
            stamp = now()
            conn.execute("""
                INSERT INTO github_repository_acquisition
                (repository_id,inspection_status,inspection_error,updated_at)
                VALUES(?,'error',?,?)
                ON CONFLICT(repository_id) DO UPDATE SET
                  inspection_status='error',inspection_error=excluded.inspection_error,updated_at=excluded.updated_at
            """, (repository_id, str(exc)[:2000], stamp))
        return RedirectResponse(f"/github/{repository_id}/acquisition?inspection=error", status_code=303)


@router.post("/github/{repository_id}/acquisition/references")
def save_reference(
    repository_id: int,
    reference_type: str = Form("attribution"),
    label: str = Form(...),
    url: str = Form(""),
    instruction_text: str = Form(""),
    source_path: str = Form(""),
    required: str | None = Form(None),
):
    ensure_schema()
    allowed = {"attribution", "project", "source", "license"}
    reference_type = reference_type if reference_type in allowed else "attribution"
    label = label.strip()[:500]
    url = url.strip()[:2000]
    if url and not re.match(r"^https?://", url, re.I):
        raise HTTPException(status_code=422, detail="La URL debe comenzar por http:// o https://")
    with db() as conn:
        exists = conn.execute("SELECT id FROM github_repositories WHERE id=?", (repository_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Repositorio no encontrado")
        stamp = now()
        conn.execute("""
            INSERT INTO github_repository_references
            (repository_id,reference_type,label,url,instruction_text,source_path,source_excerpt,required,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?, ?,?,?)
            ON CONFLICT(repository_id,reference_type,label,url,source_path) DO UPDATE SET
              instruction_text=excluded.instruction_text,required=excluded.required,updated_at=excluded.updated_at
        """, (repository_id, reference_type, label, url, instruction_text.strip()[:4000],
              source_path.strip()[:500], "", 1 if required else 0, stamp, stamp))
    return RedirectResponse(f"/github/{repository_id}/acquisition", status_code=303)
