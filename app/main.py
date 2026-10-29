from __future__ import annotations

import html
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.db import connect

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
ARCHIVE_ENDPOINT = "https://archive.org/advancedsearch.php"
SCRAPE_ENDPOINT = "https://archive.org/services/search/v1/scrape"
FIELDS = ["identifier", "title", "creator", "date", "mediatype", "collection", "rights", "description", "subject", "language", "publisher", "format"]
SCRAPE_FIELDS = ",".join(FIELDS)
USER_AGENT = "OSAP-AI-Importacion/0.1 (+https://openmusicrepository.com; metadata research)"
MAX_PDF_TEXT_PAGES = 50


def count_review_cases(conn) -> int:
    """Count exactly the items the human review queue can show."""
    return int(conn.execute(
        """SELECT COUNT(DISTINCT a.identifier)
           FROM archive_items a
           JOIN global_discovery_seen s ON s.identifier=a.identifier
           WHERE s.score>0
             AND EXISTS (SELECT 1 FROM archive_files f
                         WHERE f.identifier=a.identifier AND f.download_status='downloaded')
             AND (
               EXISTS (SELECT 1 FROM archive_content_suggestions cs
                       WHERE cs.identifier=a.identifier AND cs.status='pending')
               OR (
                 NOT EXISTS (SELECT 1 FROM archive_item_contents c WHERE c.identifier=a.identifier)
                 AND (
                   EXISTS (SELECT 1 FROM archive_file_analyses fa
                           WHERE fa.identifier=a.identifier AND fa.status='ok')
                   OR EXISTS (SELECT 1 FROM archive_pdf_extractions pe
                              WHERE pe.identifier=a.identifier
                                AND pe.status IN ('text_available','ocr_completed','ocr_no_text',
                                                  'image_ocr_completed','text_checked'))
                 )
               )
               OR EXISTS (SELECT 1 FROM archive_file_analyses fa
                          WHERE fa.identifier=a.identifier AND fa.status='error')
               OR EXISTS (SELECT 1 FROM archive_pdf_extractions pe
                          WHERE pe.identifier=a.identifier
                            AND pe.status IN ('error','ocr_error','ocr_unavailable',
                                              'image_ocr_no_text','text_error'))
               OR EXISTS (SELECT 1 FROM archive_files f
                          WHERE f.identifier=a.identifier AND f.relevant=1
                            AND f.download_status='downloaded'
                            AND lower(f.file_name) NOT LIKE '%.pdf'
                            AND lower(f.file_name) NOT LIKE '%.txt'
                            AND lower(f.file_name) NOT LIKE '%.ocr'
                            AND lower(f.file_name) NOT LIKE '%.abc'
                            AND lower(f.file_name) NOT LIKE '%.ly'
                            AND lower(f.file_name) NOT LIKE '%.mei'
                            AND lower(f.file_name) NOT LIKE '%.mscx'
                            AND lower(f.file_name) NOT LIKE '%.mscz'
                            AND lower(f.file_name) NOT LIKE '%.capx'
                            AND lower(f.file_name) NOT LIKE '%.musicxml'
                            AND lower(f.file_name) NOT LIKE '%.mxl'
                            AND lower(f.file_name) NOT LIKE '%.xml'
                            AND lower(f.file_name) NOT LIKE '%.mid'
                            AND lower(f.file_name) NOT LIKE '%.midi'
                            AND lower(f.file_name) NOT LIKE '%.kar'
                            AND lower(f.file_name) NOT LIKE '%.png'
                            AND lower(f.file_name) NOT LIKE '%.jpg'
                            AND lower(f.file_name) NOT LIKE '%.jpeg'
                            AND lower(f.file_name) NOT LIKE '%.webp'
                            AND lower(f.file_name) NOT LIKE '%.tif'
                            AND lower(f.file_name) NOT LIKE '%.tiff'
                            AND lower(f.file_name) NOT LIKE '%.bmp'
                            AND lower(f.file_name) NOT LIKE '%.jp2')
             )"""
    ).fetchone()[0])


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    return connect(DB_PATH)


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
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
        CREATE TABLE IF NOT EXISTS global_discovery_queries(
          query_key TEXT PRIMARY KEY, label TEXT NOT NULL, archive_query TEXT NOT NULL,
          cursor TEXT, total_found INTEGER, scanned_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS global_discovery_seen(
          identifier TEXT PRIMARY KEY, first_query_key TEXT NOT NULL,
          score INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
          seen_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_global_discovery_seen_score ON global_discovery_seen(score);
        CREATE TABLE IF NOT EXISTS archive_item_inventories(
          identifier TEXT PRIMARY KEY REFERENCES archive_items(identifier) ON DELETE CASCADE,
          checked_at TEXT NOT NULL, file_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL, error_text TEXT);
        CREATE INDEX IF NOT EXISTS idx_archive_item_inventories_status ON archive_item_inventories(status);
        CREATE TABLE IF NOT EXISTS archive_file_analyses(
          file_id INTEGER PRIMARY KEY REFERENCES archive_files(id) ON DELETE CASCADE,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          analysis_type TEXT NOT NULL,
          status TEXT NOT NULL,
          analysis_json TEXT NOT NULL,
          error_text TEXT,
          analyzed_at TEXT NOT NULL,
          sha256 TEXT);
        CREATE INDEX IF NOT EXISTS idx_archive_file_analyses_identifier ON archive_file_analyses(identifier);
        CREATE TABLE IF NOT EXISTS archive_pdf_extractions(
          file_id INTEGER PRIMARY KEY REFERENCES archive_files(id) ON DELETE CASCADE,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          status TEXT NOT NULL,
          page_count INTEGER NOT NULL DEFAULT 0,
          extracted_characters INTEGER NOT NULL DEFAULT 0,
          error_text TEXT,
          analyzed_at TEXT NOT NULL,
          sha256 TEXT);
        CREATE INDEX IF NOT EXISTS idx_archive_pdf_extractions_identifier ON archive_pdf_extractions(identifier);
        CREATE TABLE IF NOT EXISTS capture_jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          status TEXT NOT NULL DEFAULT 'running',
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          iterations INTEGER NOT NULL DEFAULT 0,
          message TEXT NOT NULL DEFAULT '',
          error_text TEXT
        );
        CREATE TABLE IF NOT EXISTS archive_rights_reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          identifier TEXT NOT NULL REFERENCES archive_items(identifier) ON DELETE CASCADE,
          status TEXT NOT NULL,
          evidence_url TEXT NOT NULL DEFAULT '',
          basis TEXT NOT NULL DEFAULT '',
          jurisdiction TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          reviewed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_archive_rights_reviews_identifier
          ON archive_rights_reviews(identifier, reviewed_at DESC);
        CREATE TABLE IF NOT EXISTS content_analysis_jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          identifier TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          queued_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          error_text TEXT
        );
        """)
        analysis_job_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(content_analysis_jobs)")
        }
        if "identifier" not in analysis_job_columns:
            conn.execute("ALTER TABLE content_analysis_jobs ADD COLUMN identifier TEXT")
        conn.execute(
            """UPDATE capture_jobs SET status='pending', updated_at=?, completed_at=NULL, message=?
               WHERE status='running'""",
            (now(), "Recopilación reencolada al iniciar la aplicación; se continuará desde el progreso guardado."),
        )
        conn.execute(
            """UPDATE content_analysis_jobs SET status='pending',updated_at=?,completed_at=NULL
               WHERE status='running'""",
            (now(),),
        )
        stale_xml = conn.execute(
            """SELECT fa.file_id FROM archive_file_analyses fa
               JOIN archive_files f ON f.id=fa.file_id
               WHERE fa.status='error'
                 AND fa.error_text='El XML no tiene la estructura de una partitura MusicXML.'
                 AND lower(f.file_name) LIKE '%.xml'
                 AND lower(f.file_name) NOT LIKE '%.musicxml'"""
        ).fetchall()
        for row in stale_xml:
            conn.execute(
                """UPDATE archive_file_analyses
                   SET status='not_musicxml',analysis_json=?,error_text=NULL
                   WHERE file_id=?""",
                (
                    json.dumps({
                        "format": "xml",
                        "status": "not_musicxml",
                        "message": "XML válido, pero no es una partitura MusicXML.",
                    }, ensure_ascii=False),
                    row["file_id"],
                ),
            )
        conn.execute(
            """UPDATE archive_files SET relevant=0
               WHERE lower(file_name) LIKE '%.xml'
                 AND (
                   lower(file_name) LIKE '%_files.xml'
                   OR lower(file_name) LIKE '%_meta.xml'
                   OR lower(file_name) LIKE '%_metadata.xml'
                   OR lower(file_name) LIKE '%_contents.xml'
                   OR lower(COALESCE(archive_format,'')) IN ('metadata','marc','dublin core','contents')
                 )"""
        )
        conn.execute(
            """UPDATE archive_files SET relevant=0
               WHERE lower(file_name) LIKE '%thumb%'
                  OR lower(file_name) LIKE '%thumbnail%'
                  OR lower(file_name) LIKE '%tile%'
                  OR lower(file_name) LIKE '%scandata%'
                  OR lower(COALESCE(archive_format,'')) LIKE '%single page processed jp2%'
                  OR lower(COALESCE(archive_format,'')) LIKE '%single page original jp2%'
                  OR lower(COALESCE(archive_format,'')) LIKE '%jp2 zip%'
                  OR lower(COALESCE(archive_format,'')) LIKE '%jp2 tar%'"""
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    workers = [
        asyncio.create_task(capture_job_queue()),
        asyncio.create_task(content_analysis_job_queue()),
    ]
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


app = FastAPI(title="AI Importación", version="0.2.0", lifespan=lifespan)

from app.file_catalog import (
    MAX_DOWNLOAD_BYTES, _intake_priority, download_file, inventory_item_files,
    router as file_catalog_router, relevant_file, safe_filename, valid_identifier,
    METADATA_ENDPOINT, USER_AGENT,
)
from urllib.parse import quote, urlencode
from app.musicxml_analysis import NotMusicXMLFileError, analyze_musicxml
from app.midi_analysis import analyze_midi
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

async def archive_scrape(query: str, count: int, cursor: str | None = None, query_key: str = "default") -> dict:
    """Fetch one resumable batch from Internet Archive cursor-based Scraping API.

    Keep a stable query-specific field ordering to avoid observed cross-query
    response-cache collisions when request shapes are otherwise identical.
    """
    fields = list(FIELDS)
    rotations = {key: index for index, (key, _, _) in enumerate(GLOBAL_DISCOVERY_QUERIES)}
    offset = rotations.get(query_key, 0) % len(fields)
    fields = fields[offset:] + fields[:offset]
    params = {"q": query, "fields": ",".join(fields), "count": max(100, min(int(count), 1000))}
    if cursor:
        params["cursor"] = cursor
    async with httpx.AsyncClient(timeout=45, headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(SCRAPE_ENDPOINT, params=params)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        raise RuntimeError("Respuesta de exploración no válida de Internet Archive")
    return {
        "total": int(payload.get("total", 0)),
        "items": payload.get("items", []),
        "cursor": payload.get("cursor"),
    }

GLOBAL_DISCOVERY_QUERIES = (
    ("scores_titles", "Títulos con términos de partitura",
     '(title:"sheet music" OR title:"musical score" OR title:"music score" OR title:"partitura" OR title:"partituras" OR title:"vocal score" OR title:"choral score")'),
    ("scores_subjects", "Materias musicales y notación",
     '(subject:"sheet music" OR subject:"musical scores" OR subject:"music notation" OR subject:"choral music" OR subject:"hymns" OR subject:"scores")'),
    ("notation_formats", "Formatos y programas de notación",
     '(title:musicxml OR description:musicxml OR title:lilypond OR description:lilypond OR format:MusicXML OR format:MXL OR title:"musical notation")'),
    ("vocal_music", "Música vocal, coral y litúrgica",
     '(title:choir OR title:choral OR title:hymn OR title:motet OR title:madrigal OR title:cantata OR title:psalm OR title:mass OR title:opera OR title:oratorio OR subject:"vocal music")'),
    ("instrumental_music", "Música instrumental y partituras",
     '(title:sonata OR title:symphony OR title:concerto OR title:quartet OR title:trio OR title:"piano score" OR title:"orchestral score" OR subject:"instrumental music")'),
    ("file_formats", "Formatos de partituras y archivos musicales",
     '(format:PDF OR format:MIDI OR format:MusicXML OR format:MXL OR format:Sibelius OR format:Finale OR format:"Guitar Pro" OR format:"Compressed Score" OR format:DJVU OR format:PostScript OR format:ABC OR format:LilyPond OR format:Capella)'),
)


def ensure_global_discovery_queries() -> None:
    with db() as conn:
        for query_key, label, archive_query in GLOBAL_DISCOVERY_QUERIES:
            conn.execute(
                """INSERT OR IGNORE INTO global_discovery_queries
                   (query_key,label,archive_query,status,updated_at) VALUES(?,?,?,'pending',?)""",
                (query_key, label, archive_query, now()),
            )


GLOBAL_MUSIC_QUERY = '(title:"sheet music" OR title:"musical score" OR title:"partitura" OR title:"partituras" OR title:"choral score" OR title:"vocal score" OR subject:"sheet music" OR subject:"musical scores" OR subject:"music notation" OR subject:"choral music" OR subject:"partitions musicales" OR description:"sheet music")'

MUSIC_POSITIVE_TERMS = (
    "sheet music", "musical score", "music score", "score", "scores", "partitura",
    "partituras", "notenschrift", "noten", "choral", "chorus", "choir", "hymn",
    "hymns", "vocal score", "piano score", "music notation", "musicxml", "lilypond",
    "cantata", "motet", "madrigal", "mass", "missa", "psalm", "symphony", "sonata",
    "concerto", "quartet", "trio", "opera", "oratorio", "sheet-music"
)
MUSIC_NEGATIVE_TERMS = (
    "video game", "minecraft", "software", "dvd cover", "movie poster", "politics",
    "political", "casino", "scientific review", "systematic review", "medical",
    "glioblastoma", "news broadcast", "podcast", "house music mix", "dj mix"
)


def music_candidate_score(doc: dict) -> tuple[int, str]:
    """Metadata triage that deprioritizes recordings without rejecting score collections."""
    fields = []
    for key in ("title", "subject", "description", "collection", "format"):
        value = doc.get(key)
        fields.extend(value if isinstance(value, list) else [value] if value else [])
    text = " ".join(str(value) for value in fields).casefold()
    title_value = doc.get("title") or ""
    title_text = " ".join(title_value) if isinstance(title_value, list) else str(title_value)
    title_text = title_text.casefold()
    if any(term in text for term in MUSIC_NEGATIVE_TERMS):
        return 0, "Descartado por indicios de contenido ajeno a partituras"

    strong = {"sheet music", "musical score", "music score", "partitura", "partituras",
              "vocal score", "music notation", "musicxml", "lilypond"}
    title_has_score_evidence = any(term in title_text for term in strong)
    media_value = doc.get("mediatype") or ""
    media_types = media_value if isinstance(media_value, list) else [media_value]
    media_types = {str(value).casefold() for value in media_types}
    non_score_media = bool(media_types & {"audio", "movies", "video", "software", "web"})
    if non_score_media and not title_has_score_evidence:
        return 0, "Descartado provisionalmente: tipo de material audiovisual/software sin indicio explícito de partitura en el título"

    hits = [term for term in MUSIC_POSITIVE_TERMS if term in text]
    if not hits:
        return 0, "Sin indicios musicales suficientes en los metadatos"
    # Generic genres (symphony, sonata, concerto...) are weaker evidence than
    # an explicit score/notation term. Texts and collections remain eligible.
    score = sum(3 if term in strong else 1 for term in hits)
    return score, "Indicios: " + ", ".join(hits[:6])


def refresh_global_discovery_scores() -> None:
    """Re-score previously saved candidates with the current filter, without new network calls."""
    with db() as conn:
        rows = conn.execute(
            """SELECT s.identifier, a.metadata_json
               FROM global_discovery_seen s
               JOIN archive_items a ON a.identifier=s.identifier"""
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            score, reason = music_candidate_score(metadata)
            conn.execute(
                "UPDATE global_discovery_seen SET score=?,reason=? WHERE identifier=?",
                (score, reason, row["identifier"]),
            )


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


def workflow_nav(identifier: str | None = None, active: int = 1) -> str:
    """The capture workflow is a continuous process, not a five-screen wizard."""
    return ""


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
    .workflow{background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:1rem;margin:1rem 0}.workflow-steps{display:flex;gap:.5rem;flex-wrap:wrap;margin:.8rem 0}.workflow-steps a{display:block;padding:.65rem .85rem;border:1px solid #cbd5e1;border-radius:8px;text-decoration:none;background:#f8fafc;color:#174f89}.workflow-steps a span{display:inline-grid;place-items:center;border-radius:50%;background:#e5ebf2;min-width:1.5rem;height:1.5rem;margin-right:.25rem}.workflow-steps a.active{background:#174f89;color:#fff;border-color:#174f89;font-weight:700}.workflow-steps a.active span{background:#fff;color:#174f89}.workflow-help{margin:.4rem 0;color:#5d6977}.section-nav{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-top:1rem;padding-top:.7rem;border-top:1px solid #e1e6eb}.section-nav a{font-weight:600}.step-number{display:inline-block;background:#e8eef5;border-radius:50%;padding:.15rem .5rem;margin-right:.35rem}
    </style>"""
    return HTMLResponse("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>" + esc(title) + "</title>" + style + "<header><b><a href='/'>AI Importación</a></b><a href='/items'>Catálogo local</a><a href='/intake'>Captación integrada</a><span>Captación independiente · sin altas en OSAP</span></header><main>" + body + "<p class='muted'>La disponibilidad de un archivo no confirma por sí sola que su reutilización esté permitida.</p></main></html>")


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-importacion"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ensure_global_discovery_queries()
    with db() as conn:
        item_count = conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0]
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM global_discovery_seen WHERE score>0"
        ).fetchone()[0]
        scanned = conn.execute(
            "SELECT COUNT(*) FROM global_discovery_seen"
        ).fetchone()[0]
        inventory_count = conn.execute(
            "SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok'"
        ).fetchone()[0]
        downloaded_count = conn.execute(
            "SELECT COUNT(*) FROM archive_files WHERE download_status='downloaded'"
        ).fetchone()[0]
        pending_count = count_review_cases(conn)
        review_items = conn.execute(
            """SELECT a.identifier,a.title,a.creator_json,a.item_date,a.rights_status
               FROM archive_items a
               WHERE EXISTS (SELECT 1 FROM archive_files f WHERE f.identifier=a.identifier AND f.download_status='downloaded')
               ORDER BY a.last_seen_at DESC LIMIT 12"""
        ).fetchall()
        query_states = conn.execute(
            "SELECT label,status,scanned_count FROM global_discovery_queries ORDER BY query_key"
        ).fetchall()
    body = "<h1>Captación de partituras</h1>"
    body += "<p>La aplicación busca en Internet Archive, identifica material probablemente musical, inventaría los archivos disponibles, descarga formatos útiles y analiza su contenido. Solo te pedirá intervenir cuando no pueda determinar qué contiene un archivo.</p>"
    body += "<section class='panel'><h2>Recopilar material musical</h2><p>Inicia una recopilación automática que continúa por sí sola, guarda el progreso y se puede reanudar si se cierra la aplicación. No necesitas ir pulsando botones por lotes.</p>"
    body += "<form method='post' action='/capture/run'><button style='font-size:1.1rem;padding:.9rem 1.2rem'>Iniciar / reanudar recopilación completa</button></form>"
    body += "<p class='muted'>Los archivos se guardan en la carpeta local de descargas y el inventario en SQLite. No se modifica OSAP. La recopilación puede tardar y algunos archivos no estarán disponibles o superarán el límite de tamaño configurado.</p></section>"
    metrics = [
        ("Elementos examinados", scanned),
        ("Candidatos musicales", candidate_count),
        ("Inventarios completados", inventory_count),
        ("Archivos descargados", downloaded_count),
        ("Casos que necesitan revisión", pending_count),
    ]
    body += "<div class='metrics'>" + "".join(
        f"<div class='metric'><span class='muted'>{label}</span><h2>{value or 0}</h2></div>"
        for label, value in metrics
    ) + "</div>"
    body += "<details class='panel'><summary>Estado de la exploración</summary><table><tr><th>Grupo de búsqueda</th><th>Examinados</th><th>Estado</th></tr>"
    body += "".join(
        f"<tr><td>{esc(q['label'])}</td><td>{q['scanned_count']}</td><td>{esc(q['status'])}</td></tr>"
        for q in query_states
    )
    body += "</table></details>"
    body += "<section class='panel'><h2>Material descargado</h2>"
    body += item_table(review_items)
    body += "<p><a href='/review'>Revisar casos dudosos</a> · <a href='/items'>Ver todo el material encontrado</a> · <a href='/intake'>Ver actividad y archivos</a></p></section>"
    body += "<details class='panel'><summary>Búsqueda manual (opcional)</summary><form method='post' action='/search'><input name='query' type='text' required placeholder='Título, compositor o término musical'> <label>Resultados <select name='rows'>" + "".join(f"<option {'selected' if n==25 else ''}>{n}</option>" for n in [10,25,50,100]) + "</select></label> <button>Buscar</button></form></details>"
    return page("Captación musical", body)


def item_table(items):
    if not items:
        return "<p>Aún no hay elementos. Ejecuta una búsqueda para empezar a recopilar metadatos.</p>"
    body = "<table><tr><th>Título / identificador</th><th>Creador de origen</th><th>Fecha</th><th>Derechos</th></tr>"
    for item in items:
        body += f"<tr><td><a href='/items/{esc(item['identifier'])}'>{esc(item['title'] or item['identifier'])}</a><br><span class='muted'>{esc(item['identifier'])}</span></td><td>{esc(item['creator_json'])}</td><td>{esc(item['item_date'] or '—')}</td><td>{esc(item['rights_status'])}</td></tr>"
    return body + "</table>"


@app.post("/discover", response_class=HTMLResponse)
async def discover(rows: Annotated[int, Form()] = 100):
    rows = max(100, min(int(rows), 500))
    ensure_global_discovery_queries()
    refresh_global_discovery_scores()
    query = "Exploración global reanudable por familias musicales"
    query_description = json.dumps(
        [{"key": key, "label": label, "query": archive_query}
         for key, label, archive_query in GLOBAL_DISCOVERY_QUERIES],
        ensure_ascii=False,
    )
    with db() as conn:
        run_id = conn.execute(
            "INSERT INTO search_runs(query_text,archive_query,rows_requested,started_at) VALUES(?,?,?,?)",
            (query, query_description, rows, now()),
        ).lastrowid
    batch_candidates = {}
    batch_received = 0
    total_reported = 0
    errors = []
    processed_queries = 0
    try:
        with db() as conn:
            states = conn.execute(
                "SELECT * FROM global_discovery_queries WHERE status!='completed' ORDER BY query_key"
            ).fetchall()
        for state in states:
            query_key = state["query_key"]
            try:
                result = await archive_scrape(state["archive_query"], rows, state["cursor"], query_key)
                docs = result["items"]
                batch_received += len(docs)
                processed_queries += 1
                if state["cursor"] is None and state["total_found"] is None:
                    total_for_query = result["total"]
                else:
                    total_for_query = state["total_found"]
                next_cursor = result["cursor"]
                if next_cursor and next_cursor == state["cursor"]:
                    raise RuntimeError("Internet Archive devolvió el mismo cursor; se detuvo esta consulta para evitar un bucle.")
                stamp = now()
                with db() as conn:
                    for doc in docs:
                        ident = str(doc.get("identifier", "")).strip()
                        if not ident:
                            continue
                        score, reason = music_candidate_score(doc)
                        conn.execute(
                            """INSERT INTO global_discovery_seen(identifier,first_query_key,score,reason,seen_at)
                               VALUES(?,?,?,?,?)
                               ON CONFLICT(identifier) DO UPDATE SET
                               score=excluded.score, reason=excluded.reason""",
                            (ident, query_key, score, reason, stamp),
                        )
                        if score > 0:
                            candidate = dict(doc)
                            candidate["_ai_music_score"] = score
                            candidate["_ai_music_reason"] = reason
                            batch_candidates[ident] = candidate
                    conn.execute(
                        """UPDATE global_discovery_queries SET cursor=?,total_found=COALESCE(total_found,?),
                           scanned_count=scanned_count+?,status=?,updated_at=? WHERE query_key=?""",
                        (next_cursor, total_for_query, len(docs),
                         "active" if next_cursor else "completed", stamp, query_key),
                    )
                total_reported += int(total_for_query or 0)
            except Exception as exc:
                errors.append(f"{state['label']}: {exc}")
                with db() as conn:
                    conn.execute(
                        "UPDATE global_discovery_queries SET status='error',updated_at=? WHERE query_key=?",
                        (now(), query_key),
                    )
        candidates = sorted(batch_candidates.values(), key=lambda doc: doc["_ai_music_score"], reverse=True)
        saved = save_results(run_id, candidates) if candidates else 0
        with db() as conn:
            summary = conn.execute(
                "SELECT COUNT(*) AS scanned, SUM(CASE WHEN score>0 THEN 1 ELSE 0 END) AS candidates FROM global_discovery_seen"
            ).fetchone()
            active = conn.execute(
                "SELECT COUNT(*) FROM global_discovery_queries WHERE status!='completed'"
            ).fetchone()[0]
            status = "error" if errors and not processed_queries else ("partial" if errors else "completed")
            error_text = "; ".join(errors)[:1000] if errors else None
            conn.execute(
                "UPDATE search_runs SET total_found=?,result_count=?,status=?,error_text=?,completed_at=? WHERE id=?",
                (total_reported, saved, status, error_text, now(), run_id),
            )
            items = conn.execute(
                """SELECT ai.* FROM archive_items ai JOIN search_run_items sri
                   ON sri.identifier=ai.identifier WHERE sri.search_run_id=?
                   ORDER BY sri.result_rank LIMIT 100""", (run_id,)
            ).fetchall()
        progress = (
            f"<p>En este avance se examinaron <b>{batch_received}</b> registros en "
            f"<b>{processed_queries}</b> consultas; {saved} candidatos de este lote superaron el filtro.</p>"
            f"<p>Acumulado local: <b>{summary['scanned']}</b> identificadores únicos examinados y "
            f"<b>{summary['candidates'] or 0}</b> con indicios musicales en metadatos.</p>"
            f"<p>Consultas todavía pendientes o activas: <b>{active}</b>.</p>"
        )
        if errors:
            progress += "<p><b>Incidencias:</b> " + esc(" | ".join(errors)) + "</p>"
        if active:
            progress += "<form method='post' action='/discover'><input type='hidden' name='rows' value='" + str(rows) + "'><button>Continuar con el siguiente lote</button></form>"
        else:
            progress += "<p><b>Las consultas configuradas han llegado al final de sus cursores.</b> Esto no garantiza que se haya localizado todo el material musical de Internet Archive.</p>"
        body = (
            "<section class='panel'><h2>Progreso de exploración global</h2>" + progress +
            "<p>Los totales de distintas consultas pueden solaparse y no equivalen a partituras únicas. Los elementos se deduplican por identificador. El filtro se basa en metadatos, no en la inspección de todos los archivos.</p>"
            "<p><a href='/'>Volver al inicio</a></p></section>"
            f"<section class='panel'><h2>Candidatos musicales de este avance</h2>{item_table(items)}</section>"
        )
        return page("Exploración global", body)
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE search_runs SET status='error',error_text=?,completed_at=? WHERE id=?",
                (str(exc)[:1000], now(), run_id),
            )
        return page("Error de exploración", f"<h1>No se pudo completar la exploración</h1><p>{esc(exc)}</p><p><a href='/'>Volver</a></p>")




@app.get("/review", response_class=HTMLResponse)
def review_uncertain_material(page_number: int = 1):
    """Show only downloaded candidates whose content still needs a human look."""
    with db() as conn:
        total_cases = count_review_cases(conn)
        total_pages = max(1, (total_cases + 49) // 50)
        page_number = max(1, min(page_number, total_pages))
        items = conn.execute(
            """SELECT a.identifier,COALESCE(a.title,a.identifier) AS title,
                      s.score,s.reason,
                      (SELECT COUNT(*) FROM archive_item_contents c
                       WHERE c.identifier=a.identifier) AS known_units,
                      (SELECT COUNT(*) FROM archive_content_suggestions cs
                       WHERE cs.identifier=a.identifier AND cs.status='pending') AS pending_suggestions,
                      (SELECT COUNT(*) FROM archive_files f
                       WHERE f.identifier=a.identifier AND f.relevant=1
                         AND f.download_status!='downloaded') AS not_downloaded_candidates,
                      (SELECT COUNT(*) FROM archive_files f
                       WHERE f.identifier=a.identifier AND f.relevant=1
                         AND f.download_status='downloaded'
                         AND lower(f.file_name) NOT LIKE '%.pdf'
                         AND lower(f.file_name) NOT LIKE '%.txt'
                         AND lower(f.file_name) NOT LIKE '%.ocr'
                         AND lower(f.file_name) NOT LIKE '%.abc'
                         AND lower(f.file_name) NOT LIKE '%.ly'
                         AND lower(f.file_name) NOT LIKE '%.mei'
                         AND lower(f.file_name) NOT LIKE '%.mscx'
                         AND lower(f.file_name) NOT LIKE '%.mscz'
                         AND lower(f.file_name) NOT LIKE '%.capx'
                         AND lower(f.file_name) NOT LIKE '%.musicxml'
                         AND lower(f.file_name) NOT LIKE '%.mxl'
                         AND lower(f.file_name) NOT LIKE '%.xml'
                         AND lower(f.file_name) NOT LIKE '%.mid'
                         AND lower(f.file_name) NOT LIKE '%.midi'
                         AND lower(f.file_name) NOT LIKE '%.kar'
                         AND lower(f.file_name) NOT LIKE '%.png'
                         AND lower(f.file_name) NOT LIKE '%.jpg'
                         AND lower(f.file_name) NOT LIKE '%.jpeg'
                         AND lower(f.file_name) NOT LIKE '%.webp'
                         AND lower(f.file_name) NOT LIKE '%.tif'
                         AND lower(f.file_name) NOT LIKE '%.tiff'
                         AND lower(f.file_name) NOT LIKE '%.bmp'
                         AND lower(f.file_name) NOT LIKE '%.jp2') AS unsupported_files,
                      (SELECT COUNT(*) FROM archive_file_analyses fa
                       WHERE fa.identifier=a.identifier AND fa.status='error') AS analysis_failures,
                      (SELECT COUNT(*) FROM archive_pdf_extractions pe
                       WHERE pe.identifier=a.identifier
                         AND pe.status IN ('error','ocr_error','ocr_unavailable','image_ocr_no_text','text_error')) AS extraction_failures
               FROM archive_items a
               JOIN global_discovery_seen s ON s.identifier=a.identifier
               WHERE s.score>0 AND EXISTS (
                   SELECT 1 FROM archive_files f
                   WHERE f.identifier=a.identifier AND f.download_status='downloaded'
               ) AND (
                   EXISTS (SELECT 1 FROM archive_content_suggestions cs
                           WHERE cs.identifier=a.identifier AND cs.status='pending')
                   OR (
                       NOT EXISTS (SELECT 1 FROM archive_item_contents c WHERE c.identifier=a.identifier)
                       AND (
                           EXISTS (SELECT 1 FROM archive_file_analyses fa
                                   WHERE fa.identifier=a.identifier AND fa.status='ok')
                           OR EXISTS (SELECT 1 FROM archive_pdf_extractions pe
                                      WHERE pe.identifier=a.identifier
                                        AND pe.status IN ('text_available','ocr_completed','ocr_no_text','image_ocr_completed','text_checked'))
                       )
                   )
                   OR EXISTS (SELECT 1 FROM archive_file_analyses fa
                              WHERE fa.identifier=a.identifier AND fa.status='error')
                   OR EXISTS (SELECT 1 FROM archive_pdf_extractions pe
                              WHERE pe.identifier=a.identifier
                                AND pe.status IN ('error','ocr_error','ocr_unavailable','image_ocr_no_text','text_error'))
                   OR EXISTS (SELECT 1 FROM archive_files f
                              WHERE f.identifier=a.identifier AND f.relevant=1
                                AND f.download_status='downloaded'
                                AND lower(f.file_name) NOT LIKE '%.pdf'
                                AND lower(f.file_name) NOT LIKE '%.txt'
                                AND lower(f.file_name) NOT LIKE '%.ocr'
                                AND lower(f.file_name) NOT LIKE '%.abc'
                                AND lower(f.file_name) NOT LIKE '%.ly'
                                AND lower(f.file_name) NOT LIKE '%.mei'
                                AND lower(f.file_name) NOT LIKE '%.mscx'
                                AND lower(f.file_name) NOT LIKE '%.mscz'
                                AND lower(f.file_name) NOT LIKE '%.capx'
                                AND lower(f.file_name) NOT LIKE '%.musicxml'
                                AND lower(f.file_name) NOT LIKE '%.mxl'
                                AND lower(f.file_name) NOT LIKE '%.xml'
                                AND lower(f.file_name) NOT LIKE '%.mid'
                                AND lower(f.file_name) NOT LIKE '%.midi'
                                AND lower(f.file_name) NOT LIKE '%.kar'
                         AND lower(f.file_name) NOT LIKE '%.png'
                         AND lower(f.file_name) NOT LIKE '%.jpg'
                         AND lower(f.file_name) NOT LIKE '%.jpeg'
                         AND lower(f.file_name) NOT LIKE '%.webp'
                         AND lower(f.file_name) NOT LIKE '%.tif'
                         AND lower(f.file_name) NOT LIKE '%.tiff'
                         AND lower(f.file_name) NOT LIKE '%.bmp'
                         AND lower(f.file_name) NOT LIKE '%.jp2')
               )
               ORDER BY pending_suggestions DESC,s.score DESC,a.last_seen_at DESC LIMIT 50 OFFSET ?""",
            ((page_number - 1) * 50,),
        ).fetchall()
    body = "<h1>Casos que necesitan una mirada</h1>"
    body += "<p>Aquí solo aparecen materiales descargados que necesitan una decisión concreta: un posible título, un error al leer el archivo o un formato que la aplicación aún no sabe interpretar. Que un archivo esté disponible para descargar no es motivo de revisión.</p>"
    if total_cases:
        first_case = (page_number - 1) * 50 + 1
        last_case = min(page_number * 50, total_cases)
        body += f"<p>Casos {first_case}–{last_case} de {total_cases} · página {page_number} de {total_pages}</p>"
        if total_pages > 1:
            if page_number > 1:
                body += f"<a href='/review?page_number={page_number - 1}'>← Anteriores</a> "
            if page_number < total_pages:
                body += f"<a href='/review?page_number={page_number + 1}'>Siguientes →</a>"
    if not items:
        body += "<section class='panel'><p>No hay casos pendientes en este momento. Puedes continuar la captación automática.</p><p><a href='/'>Volver a captación</a></p></section>"
        return page("Revisión de material dudoso", body)
    for item in items:
        identifier = item["identifier"]
        with db() as conn:
            files = conn.execute(
                """SELECT id,file_name,archive_format,size_bytes,download_status,relevant
                   FROM archive_files WHERE identifier=? AND download_status='downloaded' AND relevant=1
                   ORDER BY relevant DESC,size_bytes,file_name""",
                (identifier,),
            ).fetchall()
            suggestions = conn.execute(
                """SELECT source_file,line_number,suggestion_text FROM archive_content_suggestions
                   WHERE identifier=? AND status='pending' ORDER BY source_file,line_number LIMIT 8""",
                (identifier,),
            ).fetchall()
        body += "<section class='panel'><h2>" + esc(item["title"]) + "</h2>"
        body += "<p><a href='/items/" + esc(identifier) + "'>Ficha de Internet Archive</a> · <a href='/items/" + esc(identifier) + "/content-analysis'>Ver análisis completo</a></p>"
        if item["pending_suggestions"]:
            review_reason = f"Hay {item['pending_suggestions']} posible(s) título(s) extraído(s) que aún no se han confirmado."
        elif item["analysis_failures"]:
            review_reason = f"No se ha podido leer correctamente uno o más archivos ({item['analysis_failures']} error(es) de análisis)."
        elif item["extraction_failures"]:
            review_reason = f"No se ha podido extraer texto de uno o más archivos ({item['extraction_failures']} error(es) o limitación(es) de lectura)."
        elif item["unsupported_files"]:
            review_reason = f"Hay {item['unsupported_files']} archivo(s) musical(es) descargado(s) cuyo formato todavía no puede interpretar automáticamente."
        elif item["known_units"] == 0:
            review_reason = "El archivo se ha analizado, pero no se ha podido identificar con seguridad una obra concreta."
        else:
            review_reason = "Hay información extraída que requiere confirmación."
        body += "<p><b>Por qué requiere revisión:</b> " + esc(review_reason) + "</p>"
        body += "<p class='muted'>Indicios musicales de origen: " + esc(item["reason"] or "Metadatos musicales") + ".</p>"
        body += "<h3>Archivos descargados para consultar</h3><ul>"
        for file_row in files:
            label = esc(file_row["file_name"]) + " — " + esc(file_row["archive_format"] or "formato no indicado")
            if file_row["size_bytes"] is not None:
                label += " (" + esc(f"{file_row['size_bytes']:,}") + " bytes)"
            suffix = Path(file_row["file_name"].lower()).suffix
            action = "Vista previa" if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".txt", ".ocr"} else "Abrir / descargar"
            body += "<li>" + label + " · <a href='/files/view/" + str(file_row["id"]) + "'>" + action + "</a></li>"
        body += "</ul>"
        if item["not_downloaded_candidates"]:
            body += "<p class='muted'>Además, hay " + str(item["not_downloaded_candidates"]) + " archivo(s) candidato(s) todavía no descargado(s). Eso no es una duda de identificación. <a href='/files/" + esc(identifier) + "'>Consultar inventario y descargas</a>.</p>"
        if suggestions:
            body += "<h3>Indicios extraídos automáticamente</h3><ul>"
            body += "".join("<li>" + esc(s["suggestion_text"]) + " <span class='muted'>(" + esc(s["source_file"]) + ", línea/página " + str(s["line_number"]) + ")</span></li>" for s in suggestions)
            body += "</ul>"
        body += "<p class='muted'>Estas pistas no se consideran una identificación definitiva. Si no basta con la vista previa, el análisis completo conserva las evidencias y permite registrar la unidad musical.</p></section>"
    body += "<p><a href='/'>Volver a captación</a></p>"
    return page("Revisión de material dudoso", body)


async def run_full_capture_job(job_id: int) -> None:
    """Run resumable Archive.org discovery, file inventory and downloads until exhausted or stalled."""
    from app.file_catalog import run_integrated_intake
    no_progress_rounds = 0
    last_snapshot = None
    last_errors: list[str] = []
    try:
        for iteration in range(1, 100001):
            with db() as conn:
                before = conn.execute("""SELECT
                    (SELECT COALESCE(SUM(scanned_count),0) FROM global_discovery_queries) AS scanned,
                    (SELECT COUNT(*) FROM global_discovery_queries WHERE status='completed') AS completed_queries,
                    (SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok') AS inventories,
                    (SELECT COUNT(*) FROM archive_files WHERE download_status='downloaded') AS downloads,
                    (SELECT COUNT(*) FROM global_discovery_seen WHERE score>0) AS candidates""").fetchone()
                pending_inventory = conn.execute("""SELECT COUNT(*) FROM global_discovery_seen s
                    LEFT JOIN archive_item_inventories i ON i.identifier=s.identifier
                    WHERE s.score>0 AND (i.identifier IS NULL OR i.status='error')""").fetchone()[0]
                pending_downloads = conn.execute("""SELECT COUNT(*) FROM archive_files f
                    JOIN global_discovery_seen s ON s.identifier=f.identifier
                    JOIN archive_item_inventories i ON i.identifier=f.identifier AND i.status='ok'
                    WHERE s.score>0 AND f.relevant=1 AND f.download_status IN ('not_downloaded','error')
                    AND (f.size_bytes IS NULL OR f.size_bytes<=?)""",
                    (int(os.getenv("AI_IMPORTACION_MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024))),)).fetchone()[0]
                query_states = conn.execute("SELECT status FROM global_discovery_queries").fetchall()
            snapshot = (before["scanned"], before["completed_queries"], before["inventories"], before["downloads"], before["candidates"], pending_inventory, pending_downloads)
            if snapshot == last_snapshot:
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0
            last_snapshot = snapshot

            if all(row["status"] == "completed" for row in query_states) and pending_inventory == 0 and pending_downloads == 0:
                with db() as conn:
                    conn.execute("UPDATE capture_jobs SET status='completed',updated_at=?,completed_at=?,iterations=?,message=?,error_text=? WHERE id=?",
                                 (now(), now(), iteration, "La búsqueda configurada ha terminado y todos los archivos descargables dentro de los límites se han guardado.", "; ".join(last_errors[-10:]) or None, job_id))
                return
            if no_progress_rounds >= 3:
                with db() as conn:
                    conn.execute("UPDATE capture_jobs SET status='completed_with_errors',updated_at=?,completed_at=?,iterations=?,message=?,error_text=? WHERE id=?",
                                 (now(), now(), iteration, "La recopilación se ha detenido porque Internet Archive no permite avanzar tras varios intentos. El progreso y los archivos obtenidos se conservan; quedan incidencias registradas.", "; ".join(last_errors[-10:]) or "Sin progreso en tres ciclos; quedan elementos pendientes.", job_id))
                return

            ensure_global_discovery_queries()
            if not all(row["status"] == "completed" for row in query_states):
                try:
                    await discover(500)
                except Exception as exc:
                    last_errors.append("Búsqueda: " + str(exc)[:180])
            try:
                await run_integrated_intake(items=20, downloads=10)
            except Exception as exc:
                last_errors.append("Inventario/descarga: " + str(exc)[:180])
            # run_integrated_intake already invokes the automatic analyzer after downloads.

            with db() as conn:
                conn.execute("UPDATE capture_jobs SET updated_at=?,iterations=?,message=?,error_text=? WHERE id=? AND status='running'",
                             (now(), iteration, "Recopilación automática en curso. El avance se guarda en el equipo.", "; ".join(last_errors[-10:]) or None, job_id))
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE capture_jobs SET status='error',updated_at=?,completed_at=?,message=?,error_text=? WHERE id=?",
                         (now(), now(), "La recopilación se interrumpió por un error. Los archivos ya descargados y el avance de búsqueda se conservan.", str(exc)[:2000], job_id))


async def run_queued_capture_job(job_id: int) -> bool:
    with db() as conn:
        claimed = conn.execute(
            """UPDATE capture_jobs SET status='running',updated_at=?,completed_at=NULL
               WHERE id=? AND status='pending'""",
            (now(), job_id),
        ).rowcount
    if not claimed:
        return False
    try:
        await run_full_capture_job(job_id)
    except asyncio.CancelledError:
        with db() as conn:
            conn.execute(
                """UPDATE capture_jobs SET status='pending',updated_at=?,completed_at=NULL,message=?
                   WHERE id=? AND status='running'""",
                (now(), "Recopilación pausada al cerrar la aplicación; se reanudará al volver a iniciar.", job_id),
            )
        raise
    return True


async def capture_job_queue() -> None:
    """Run durable queued jobs sequentially."""
    while True:
        with db() as conn:
            job = conn.execute(
                "SELECT id FROM capture_jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
        if not job:
            await asyncio.sleep(1)
            continue
        await run_queued_capture_job(int(job["id"]))


def enqueue_content_analysis(identifier: str | None = None) -> int:
    with db() as conn:
        pending = conn.execute(
            """SELECT id FROM content_analysis_jobs
               WHERE status IN ('pending','running') AND identifier IS ?
               ORDER BY id DESC LIMIT 1""",
            (identifier,),
        ).fetchone()
        if pending:
            return int(pending["id"])
        return int(conn.execute(
            """INSERT INTO content_analysis_jobs(identifier,status,queued_at,updated_at)
               VALUES(?,'pending',?,?)""",
            (identifier, now(), now()),
        ).lastrowid)


async def run_queued_content_analysis(job_id: int) -> bool:
    with db() as conn:
        claimed = conn.execute(
            """UPDATE content_analysis_jobs SET status='running',updated_at=?,completed_at=NULL
               WHERE id=? AND status='pending'""",
            (now(), job_id),
        ).rowcount
    if not claimed:
        return False
    try:
        with db() as conn:
            job = conn.execute(
                "SELECT identifier FROM content_analysis_jobs WHERE id=?", (job_id,)
            ).fetchone()
        identifier = job["identifier"] if job else None
        if identifier:
            await run_in_threadpool(process_downloaded_batch, identifier)
        else:
            await run_in_threadpool(process_downloaded_batch)
    except asyncio.CancelledError:
        with db() as conn:
            conn.execute(
                """UPDATE content_analysis_jobs SET status='pending',updated_at=?,completed_at=NULL
                   WHERE id=? AND status='running'""",
                (now(), job_id),
            )
        raise
    except Exception as exc:
        with db() as conn:
            conn.execute(
                """UPDATE content_analysis_jobs SET status='error',updated_at=?,completed_at=?,error_text=?
                   WHERE id=?""",
                (now(), now(), str(exc)[:2000], job_id),
            )
        return True
    with db() as conn:
        conn.execute(
            """UPDATE content_analysis_jobs SET status='completed',updated_at=?,completed_at=?,error_text=NULL
               WHERE id=?""",
            (now(), now(), job_id),
        )
    return True


async def content_analysis_job_queue() -> None:
    """Process durable analysis jobs independently from download and web request flows."""
    while True:
        with db() as conn:
            job = conn.execute(
                "SELECT id FROM content_analysis_jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
        if not job:
            await asyncio.sleep(1)
            continue
        await run_queued_content_analysis(int(job["id"]))


@app.post("/capture/run", response_class=HTMLResponse)
async def run_capture_pipeline():
    """Start one resumable full collection job; the user does not need to run batches manually."""
    ensure_global_discovery_queries()
    with db() as conn:
        active = conn.execute(
            "SELECT id FROM capture_jobs WHERE status IN ('pending','running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            job_id = int(active["id"])
        else:
            job_id = int(conn.execute(
                "INSERT INTO capture_jobs(status,started_at,updated_at,message) VALUES('pending',?,?,?)",
                (now(), now(), "Recopilación automática iniciada."),
            ).lastrowid)
    return RedirectResponse(url=f"/capture/status/{job_id}", status_code=303)


@app.get("/capture/status/{job_id}", response_class=HTMLResponse)
def capture_status(job_id: int):
    with db() as conn:
        job = conn.execute("SELECT * FROM capture_jobs WHERE id=?", (job_id,)).fetchone()
        totals = conn.execute("""SELECT
            (SELECT COUNT(*) FROM global_discovery_seen) AS scanned,
            (SELECT COUNT(*) FROM global_discovery_seen WHERE score>0) AS candidates,
            (SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok') AS inventoried,
            (SELECT COUNT(*) FROM archive_files WHERE download_status='downloaded') AS downloaded,
            (SELECT COUNT(*) FROM archive_files WHERE download_status='error') AS download_errors""").fetchone()
        pending_inventory = conn.execute("""SELECT COUNT(*) FROM global_discovery_seen s
            LEFT JOIN archive_item_inventories i ON i.identifier=s.identifier
            WHERE s.score>0 AND (i.identifier IS NULL OR i.status='error')""").fetchone()[0]
        pending_downloads = conn.execute("""SELECT COUNT(*) FROM archive_files f
            JOIN global_discovery_seen s ON s.identifier=f.identifier
            JOIN archive_item_inventories i ON i.identifier=f.identifier AND i.status='ok'
            WHERE s.score>0 AND f.relevant=1 AND f.download_status IN ('not_downloaded','error')""").fetchone()[0]
        review_count = count_review_cases(conn)
        query_states = conn.execute("SELECT label,status,scanned_count FROM global_discovery_queries ORDER BY query_key").fetchall()
    if not job:
        raise HTTPException(status_code=404, detail="Recopilación no encontrada")
    running = job["status"] in {"pending", "running"}
    refresh = "<meta http-equiv='refresh' content='15'>" if running else ""
    body = "<h1>" + ("Recopilación automática en curso" if running else "Resultado de la recopilación") + "</h1>"
    if running:
        body += "<p>" + ("Recopilación en cola; comenzará automáticamente." if job["status"] == "pending" else "La aplicación está buscando y guardando archivos por sí sola.") + " No hace falta pulsar nada ni permanecer en esta pantalla; puedes volver más tarde a esta dirección.</p>"
    else:
        body += "<p>" + esc(job["message"]) + "</p>"
    body += "<div class='metrics'>" + "".join(
        f"<div class='metric'><span class='muted'>{label}</span><h2>{value or 0}</h2></div>"
        for label, value in [
            ("Elementos examinados", totals["scanned"]), ("Candidatos musicales", totals["candidates"]),
            ("Elementos inventariados", totals["inventoried"]), ("Archivos guardados", totals["downloaded"]),
            ("Descargas con error", totals["download_errors"]), ("Casos que necesitan revisión", review_count),
        ]) + "</div>"
    if not running:
        body += "<p><b>Estado:</b> " + esc(job["status"]) + " · <b>Ciclos realizados:</b> " + str(job["iterations"]) + "</p>"
        if job["error_text"]:
            body += "<details class='panel'><summary>Incidencias registradas</summary><pre>" + esc(job["error_text"]) + "</pre></details>"
        body += "<p>Los archivos están guardados en la carpeta local de descargas, con su procedencia y SHA-256 en la base de datos local. No se ha modificado OSAP.</p>"
    else:
        body += "<p class='muted'>Última actualización: " + esc(job["updated_at"]) + ". La base local conserva el progreso.</p>"
    body += "<p><a href='/items'>Ver material encontrado</a> · <a href='/intake'>Ver archivos guardados</a> · <a href='/review'>Ver casos dudosos</a> · <a href='/'>Inicio</a></p>"
    body += "<details class='panel'><summary>Detalle técnico (opcional)</summary><table><tr><th>Grupo</th><th>Estado</th><th>Examinados</th></tr>" + "".join(
        f"<tr><td>{esc(row['label'])}</td><td>{esc(row['status'])}</td><td>{row['scanned_count']}</td></tr>" for row in query_states
    ) + "</table><p>Inventarios pendientes o con error: " + str(pending_inventory) + " · Archivos pendientes o con error: " + str(pending_downloads) + "</p></details>"
    document = page("Recopilación musical", body).body.decode("utf-8")
    if running:
        document = document.replace("<meta charset='utf-8'>", "<meta charset='utf-8'><meta http-equiv='refresh' content='15'>", 1)
    return HTMLResponse(document)

@app.post("/inventory-candidates", response_class=HTMLResponse)
async def inventory_candidates(batch_size: Annotated[int, Form()] = 25):
    batch_size = max(1, min(int(batch_size), 50))
    with db() as conn:
        targets = conn.execute("""SELECT a.identifier, a.title
            FROM global_discovery_seen s JOIN archive_items a ON a.identifier=s.identifier
            LEFT JOIN archive_item_inventories i ON i.identifier=s.identifier
            WHERE s.score>0 AND (i.identifier IS NULL OR i.status='error')
            ORDER BY s.score DESC, s.seen_at DESC LIMIT ?""", (batch_size,)).fetchall()
    succeeded = 0
    failed = []
    total_files = 0
    for target in targets:
        identifier = target["identifier"]
        if not valid_identifier(identifier):
            message = "Identificador de Internet Archive no válido"
            with db() as conn:
                conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                    VALUES(?,?,0,'error',?) ON CONFLICT(identifier) DO UPDATE SET
                    checked_at=excluded.checked_at,status='error',error_text=excluded.error_text""",
                    (identifier, now(), message))
            failed.append(f"{identifier}: {message}")
            continue
        try:
            async with httpx.AsyncClient(timeout=45, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
                response = await client.get(METADATA_ENDPOINT + quote(identifier, safe=""))
                response.raise_for_status()
                payload = response.json()
            files = payload.get("files", [])
            if not isinstance(files, list):
                raise ValueError("Respuesta de inventario no válida: files no es una lista")
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
                    source_url = "https://archive.org/download/" + quote(identifier, safe="") + "/" + quote(name, safe="/")
                    conn.execute("""INSERT INTO archive_files
                      (identifier,file_name,file_name_safe,source_url,size_bytes,archive_format,md5,sha1,relevant,inventory_seen_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(identifier,file_name) DO UPDATE SET file_name_safe=excluded.file_name_safe,
                      source_url=excluded.source_url,size_bytes=excluded.size_bytes,archive_format=excluded.archive_format,
                      md5=excluded.md5,sha1=excluded.sha1,relevant=excluded.relevant,inventory_seen_at=excluded.inventory_seen_at""",
                      (identifier, name, safe_filename(name), source_url, size, fmt, item.get("md5"), item.get("sha1"),
                       int(relevant_file(name, fmt)), stamp))
                    recorded += 1
                conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                    VALUES(?,?,?,'ok',NULL) ON CONFLICT(identifier) DO UPDATE SET
                    checked_at=excluded.checked_at,file_count=excluded.file_count,status='ok',error_text=NULL""",
                    (identifier, stamp, recorded))
            succeeded += 1
            total_files += recorded
        except Exception as exc:
            message = str(exc)[:1000]
            with db() as conn:
                conn.execute("""INSERT INTO archive_item_inventories(identifier,checked_at,file_count,status,error_text)
                    VALUES(?,?,0,'error',?) ON CONFLICT(identifier) DO UPDATE SET
                    checked_at=excluded.checked_at,status='error',error_text=excluded.error_text""",
                    (identifier, now(), message))
            failed.append(f"{identifier}: {message}")
    with db() as conn:
        remaining = conn.execute("""SELECT COUNT(*) FROM global_discovery_seen s
            LEFT JOIN archive_item_inventories i ON i.identifier=s.identifier
            WHERE s.score>0 AND (i.identifier IS NULL OR i.status='error')""").fetchone()[0]
        checked = conn.execute("SELECT COUNT(*) FROM archive_item_inventories WHERE status='ok'").fetchone()[0]
        file_total = conn.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0]
        formats = conn.execute("""SELECT COALESCE(NULLIF(archive_format,''),'Formato no indicado') AS fmt, COUNT(*) AS n
            FROM archive_files GROUP BY fmt ORDER BY n DESC LIMIT 20""").fetchall()
    body = "<section class='panel'><h1>Inventario de archivos</h1><p>En este lote se han consultado <b>" + str(len(targets)) + "</b> elementos: <b>" + str(succeeded) + "</b> inventarios correctos, <b>" + str(len(failed)) + "</b> con error y <b>" + str(total_files) + "</b> archivos registrados en los inventarios correctos.</p><p>Elementos con inventario correcto acumulados: <b>" + str(checked) + "</b>. Archivos registrados en total: <b>" + str(file_total) + "</b>. Pendientes o reintentables: <b>" + str(remaining) + "</b>.</p>"
    if failed:
        body += "<h3>Errores que se podrán reintentar</h3><ul>" + "".join("<li>" + esc(error) + "</li>" for error in failed[:20]) + "</ul>"
    body += "<h3>Formatos registrados (incluidos los no reconocidos)</h3><table><tr><th>Formato declarado</th><th>Archivos</th></tr>" + "".join(f"<tr><td>{esc(row['fmt'])}</td><td>{row['n']}</td></tr>" for row in formats) + "</table>"
    if remaining:
        body += "<form method='post' action='/inventory-candidates'><input type='hidden' name='batch_size' value='" + str(batch_size) + "'><button>Continuar con el siguiente lote</button></form>"
    body += "<p><a href='/'>Volver al inicio</a></p></section>"
    return page("Inventario de archivos", body)


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
        body = workflow_nav(None, 1)
        body += ("<section class='panel'><h1>No se pudo completar la búsqueda</h1>"
                 "<p>Internet Archive no ha respondido correctamente. Los resultados de esta búsqueda no se han guardado como completados; puedes volver al buscador e intentarlo de nuevo.</p>"
                 f"<p class='muted'>Detalle técnico: {esc(exc)}</p>"
                 "<p><a href='/'>← Volver al buscador y reintentar</a></p></section>")
        return page("Error de búsqueda", body)
    with db() as conn:
        items = conn.execute("""SELECT ai.* FROM archive_items ai
            JOIN search_run_items sri ON sri.identifier=ai.identifier
            WHERE sri.search_run_id=? ORDER BY sri.result_rank LIMIT 100""", (run_id,)).fetchall()
    body = workflow_nav(None, 2)
    body += f"<section class='panel'><h2>Resultados de búsqueda: {esc(query)}</h2><p>Internet Archive informa de <b>{result['total']}</b> coincidencias. Se han recibido y guardado <b>{saved}</b> elementos en esta búsqueda.</p><p>Elige un resultado para abrir su ficha y continuar con el inventario de archivos.</p><p><a href='/'>← Volver a buscar</a></p></section><section class='panel'><h2>Resultados encontrados</h2>{item_table(items)}</section>"
    return page("Resultado de búsqueda", body)


@app.get("/items", response_class=HTMLResponse)
def items_page(q: str = "", page_number: int = 1):
    page_size = 50
    page_number = max(1, page_number)
    query = q.strip()
    with db() as conn:
        if query:
            like = "%" + query + "%"
            total = conn.execute(
                "SELECT COUNT(*) FROM archive_items WHERE title LIKE ? OR creator_json LIKE ? OR identifier LIKE ?",
                (like, like, like),
            ).fetchone()[0]
            page_count = max(1, (total + page_size - 1) // page_size)
            page_number = min(page_number, page_count)
            items = conn.execute(
                """SELECT * FROM archive_items
                   WHERE title LIKE ? OR creator_json LIKE ? OR identifier LIKE ?
                   ORDER BY last_seen_at DESC LIMIT ? OFFSET ?""",
                (like, like, like, page_size, (page_number - 1) * page_size),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0]
            page_count = max(1, (total + page_size - 1) // page_size)
            page_number = min(page_number, page_count)
            items = conn.execute(
                "SELECT * FROM archive_items ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
                (page_size, (page_number - 1) * page_size),
            ).fetchall()
    body = workflow_nav(None, 2)
    body += "<h1>Catálogo local · elige un elemento</h1><p>Abre la ficha del elemento que quieras investigar. El catálogo conserva los resultados localmente y no crea obras en OSAP.</p><form method='get'><input type='text' name='q' value='" + esc(q) + "' placeholder='Título, identificador o creador'> <button>Filtrar</button></form>"
    start = (page_number - 1) * page_size + 1 if total else 0
    end = min(page_number * page_size, total)
    body += f"<p class='muted'>Mostrando {start}–{end} de {total} elementos. Página {page_number} de {page_count}.</p><section class='panel'>" + item_table(items) + "</section>"
    if page_count > 1:
        previous = ""
        following = ""
        if page_number > 1:
            previous_url = "?" + urlencode({"q": q, "page_number": page_number - 1})
            previous = "<a href='" + esc(previous_url) + "'>← Anterior</a>"
        if page_number < page_count:
            next_url = "?" + urlencode({"q": q, "page_number": page_number + 1})
            following = "<a href='" + esc(next_url) + "'>Siguiente →</a>"
        body += "<nav class='section-nav'>" + previous + following + "</nav>"
    return page("Catálogo local", body)


@app.get("/items/{identifier}", response_class=HTMLResponse)
def item_detail(identifier: str):
    with db() as conn:
        item = conn.execute("SELECT * FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
        rights_reviews = conn.execute(
            "SELECT * FROM archive_rights_reviews WHERE identifier=? ORDER BY reviewed_at DESC,id DESC",
            (identifier,),
        ).fetchall()
    if not item:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")
    item = dict(item)
    body = workflow_nav(identifier, 2)
    body += f"<p><a href='/items'>← Volver al catálogo</a></p><h1>{esc(item['title'] or identifier)}</h1><p><a target='_blank' rel='noopener' href='{esc(item['source_url'])}'>Abrir elemento original en Internet Archive ↗</a></p>"
    body += "<section class='panel'><h2>Buscar archivos musicales</h2><p>Consulta automáticamente el inventario de Internet Archive, descarga hasta tres archivos musicales prioritarios y analiza lo que la aplicación pueda leer. No hace falta inventariar ni descargar cada archivo a mano.</p><form method='post' action='/items/" + esc(identifier) + "/analyze'><button>Buscar, descargar y analizar archivos</button></form><p><a href='/files/" + esc(identifier) + "'>Consultar archivos →</a> · <a href='/items/" + esc(identifier) + "/content-analysis'>Ver análisis de contenido</a></p></section>"
    body += "<section class='panel'><h2>Ficha local</h2>"
    for label, key in [("Identificador","identifier"),("Creador","creator_json"),("Fecha","item_date"),("Tipo","mediatype"),("Visto por primera vez","first_seen_at"),("Última observación","last_seen_at"),("Declaración de derechos de origen","rights_json")]:
        body += f"<p><b>{label}:</b> {esc(item[key] or '—')}</p>"
    latest_review = rights_reviews[0] if rights_reviews else None
    body += f"<p><b>Revisión de derechos:</b> {esc(item['rights_status'])}</p><form method='post' action='/items/{esc(identifier)}/rights-status'><select name='status'>"
    for value, label in [("unreviewed","Sin revisar"),("evidence_found","Evidencia localizada; pendiente de validar"),("public_domain_confirmed","Dominio público confirmado"),("permission_required","Se requiere permiso"),("restricted","Uso restringido"),("unclear","No está claro")]:
        body += f"<option value='{value}' {'selected' if item['rights_status']==value else ''}>{label}</option>"
    body += "</select>"
    body += "<p><label>URL de la evidencia (opcional)<br><input type='url' name='evidence_url' maxlength='2000' style='width:95%' value='" + esc(latest_review["evidence_url"] if latest_review else "") + "'></label></p>"
    body += "<p><label>Base de la decisión / licencia / fuente<br><input type='text' name='basis' maxlength='500' style='width:95%' value='" + esc(latest_review["basis"] if latest_review else "") + "'></label></p>"
    body += "<p><label>Jurisdicción (si aplica)<br><input type='text' name='jurisdiction' maxlength='120' value='" + esc(latest_review["jurisdiction"] if latest_review else "") + "'></label></p>"
    body += "<p><label>Notas de revisión<br><textarea name='notes' maxlength='4000' rows='3' style='width:95%'>" + esc(latest_review["notes"] if latest_review else "") + "</textarea></label></p>"
    body += "<p class='muted'>Registra la fuente y el razonamiento de cada revisión. El estado no constituye asesoramiento jurídico; la declaración de Archive.org por sí sola no confirma permisos.</p><button>Guardar revisión</button></form>"
    if rights_reviews:
        body += "<h3>Historial de revisiones</h3><ol>"
        for review in rights_reviews:
            body += "<li><b>" + esc(review["status"]) + "</b> · " + esc(review["reviewed_at"])
            if review["basis"]:
                body += "<br>Base: " + esc(review["basis"])
            if review["jurisdiction"]:
                body += "<br>Jurisdicción: " + esc(review["jurisdiction"])
            if review["evidence_url"]:
                body += "<br>Evidencia: <a href='" + esc(review["evidence_url"]) + "' target='_blank' rel='noopener'>" + esc(review["evidence_url"]) + "</a>"
            if review["notes"]:
                body += "<br>" + esc(review["notes"])
            body += "</li>"
        body += "</ol>"
    body += "</section><section class='panel'><h2>Metadatos originales conservados</h2><pre>" + esc(json.dumps(json.loads(item["metadata_json"]), ensure_ascii=False, indent=2)) + "</pre></section>"
    return page("Ficha " + identifier, body)


@app.post("/items/{identifier}/analyze", response_class=HTMLResponse)
async def start_item_analysis(identifier: str):
    if not valid_identifier(identifier):
        raise HTTPException(status_code=422, detail="Identificador no válido")
    with db() as conn:
        item = conn.execute(
            "SELECT title FROM archive_items WHERE identifier=?", (identifier,)
        ).fetchone()
        inventory = conn.execute(
            "SELECT status FROM archive_item_inventories WHERE identifier=?", (identifier,)
        ).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")

    try:
        if not inventory or inventory["status"] != "ok":
            await inventory_item_files(identifier)
    except Exception as exc:
        body = (
            "<h1>No se pudo consultar Internet Archive</h1>"
            "<p>No se ha podido obtener el inventario de archivos. No se inició el análisis; "
            "puedes volver a intentarlo.</p><p>Detalle: " + esc(str(exc)) + "</p>"
            "<p><a href='/items/" + quote(identifier, safe="") + "'>Volver a la ficha</a></p>"
        )
        return HTMLResponse(
            "<!doctype html><html lang='es'><meta charset='utf-8'><body>" + body + "</body></html>",
            status_code=502,
        )

    with db() as conn:
        candidates = conn.execute(
            """SELECT id,file_name,archive_format,size_bytes,download_status
               FROM archive_files WHERE identifier=? AND relevant=1
                 AND download_status IN ('not_downloaded','error')
               ORDER BY id""",
            (identifier,),
        ).fetchall()
        downloaded_count = int(conn.execute(
            """SELECT COUNT(*) FROM archive_files
               WHERE identifier=? AND relevant=1 AND download_status='downloaded'""",
            (identifier,),
        ).fetchone()[0])

    candidates = sorted(
        candidates,
        key=lambda row: _intake_priority(row["file_name"], row["archive_format"], row["size_bytes"]),
        reverse=True,
    )
    attempted = 0
    downloaded_now = 0
    for candidate in candidates:
        if attempted >= 3:
            break
        priority = _intake_priority(
            candidate["file_name"], candidate["archive_format"], candidate["size_bytes"]
        )
        if priority <= 0:
            continue
        if candidate["size_bytes"] is not None and candidate["size_bytes"] > MAX_DOWNLOAD_BYTES:
            with db() as conn:
                conn.execute(
                    "UPDATE archive_files SET download_status='too_large',download_error=? WHERE id=?",
                    ("Supera el límite configurado", candidate["id"]),
                )
            continue
        attempted += 1
        await download_file(int(candidate["id"]))
        with db() as conn:
            status = conn.execute(
                "SELECT download_status FROM archive_files WHERE id=?", (candidate["id"],)
            ).fetchone()
        if status and status["download_status"] == "downloaded":
            downloaded_now += 1

    with db() as conn:
        downloaded_count = int(conn.execute(
            """SELECT COUNT(*) FROM archive_files
               WHERE identifier=? AND relevant=1 AND download_status='downloaded'""",
            (identifier,),
        ).fetchone()[0])
        inventory_count = int(conn.execute(
            "SELECT COUNT(*) FROM archive_files WHERE identifier=?", (identifier,)
        ).fetchone()[0])
        download_issues = conn.execute(
            """SELECT file_name,download_status,download_error FROM archive_files
               WHERE identifier=? AND download_status IN ('error','too_large')
               ORDER BY id DESC LIMIT 5""",
            (identifier,),
        ).fetchall()
    if not downloaded_count:
        body = (
            "<h1>No se encontraron archivos musicales descargables</h1><p>Se consultó el inventario "
            "de Internet Archive (" + str(inventory_count) + " archivos), pero no hay archivos "
            "musicales candidatos descargados. Puede que el elemento no tenga formatos compatibles, "
            "que superen el límite de tamaño o que las descargas hayan fallado.</p>"
        )
        if download_issues:
            body += "<h2>Incidencias de descarga</h2><ul>" + "".join(
                "<li>" + esc(row["file_name"]) + " · "
                + esc(row["download_error"] or row["download_status"]) + "</li>"
                for row in download_issues
            ) + "</ul>"
        body += (
            "<p><a href='/files/" + quote(identifier, safe="") + "'>Ver inventario y estados</a> · "
            "<a href='/items/" + quote(identifier, safe="") + "'>Volver a la ficha</a></p>"
        )
        return HTMLResponse(
            "<!doctype html><html lang='es'><meta charset='utf-8'><body>" + body + "</body></html>"
        )

    job_id = enqueue_content_analysis(identifier)
    return RedirectResponse(
        url=f"/intake/analysis-status/{job_id}"
        + f"?descargados={downloaded_now}&archivos={downloaded_count}",
        status_code=303,
    )


@app.post("/items/{identifier}/rights-status")
def rights_status(
    identifier: str,
    status: Annotated[str, Form()] = "unreviewed",
    evidence_url: Annotated[str, Form()] = "",
    basis: Annotated[str, Form()] = "",
    jurisdiction: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    allowed = {"unreviewed","evidence_found","public_domain_confirmed","permission_required","restricted","unclear"}
    if status not in allowed:
        raise HTTPException(status_code=422, detail="Estado no válido")
    evidence_url = evidence_url.strip()
    try:
        parsed_url = urlsplit(evidence_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="La URL de evidencia no es válida") from exc
    if evidence_url and (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
        raise HTTPException(status_code=422, detail="La URL de evidencia debe usar http o https")
    basis = basis.strip()
    jurisdiction = jurisdiction.strip()
    notes = notes.strip()
    if len(evidence_url) > 2000 or len(basis) > 500 or len(jurisdiction) > 120 or len(notes) > 4000:
        raise HTTPException(status_code=422, detail="Uno de los campos de revisión supera su longitud máxima")
    with db() as conn:
        cur = conn.execute("UPDATE archive_items SET rights_status=? WHERE identifier=?", (status, identifier))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        conn.execute(
            """INSERT INTO archive_rights_reviews
               (identifier,status,evidence_url,basis,jurisdiction,notes,reviewed_at)
               VALUES(?,?,?,?,?,?,?)""",
            (identifier, status, evidence_url, basis, jurisdiction, notes, now()),
        )
    return RedirectResponse(url="/items/" + identifier, status_code=303)


CONTENT_SCOPES = {
    "one_work": "Una obra musical",
    "multiple_works": "Varias obras / recopilación de partituras",
    "collection": "Colección o volumen; contenido por determinar",
    "not_music": "No parece contener partituras musicales",
    "uncertain": "Pendiente de determinar",
}


@app.get("/items/{identifier}/content-analysis", response_class=HTMLResponse)
def content_analysis(identifier: str, step: int = 4, analizados: int | None = None, omitidos: int | None = None, fallidos: int | None = None, pdf_con_texto: int | None = None, pdf_ocr: int | None = None, pdf_ocr_realizados: int | None = None, pdf_ocr_sin_texto: int | None = None, errores_extraccion: int | None = None, sugerencias_nuevas: int | None = None, txt_leidos: int | None = None, notacion_leida: int | None = None, imagenes_ocr: int | None = None, imagenes_sin_texto: int | None = None, imagenes_leidas: int | None = None):
    with db() as conn:
        item = conn.execute("SELECT * FROM archive_items WHERE identifier=?", (identifier,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        assessment = conn.execute("SELECT * FROM archive_content_assessments WHERE identifier=?", (identifier,)).fetchone()
        units = conn.execute("SELECT * FROM archive_item_contents WHERE identifier=? ORDER BY ordinal", (identifier,)).fetchall()
        files = conn.execute("SELECT file_name,archive_format,size_bytes,relevant FROM archive_files WHERE identifier=? ORDER BY relevant DESC,file_name", (identifier,)).fetchall()
    scope = assessment["scope"] if assessment else "uncertain"
    rationale = assessment["rationale"] if assessment else ""
    active_step = 5 if step == 5 else 4
    body = workflow_nav(identifier, active_step)
    body += "<p class='muted'>Este flujo es independiente: no se crean ni exportan obras OSAP.</p>"
    body += f"<p><a href='/items/{esc(identifier)}'>← Volver a la ficha</a></p><h1>Análisis del contenido: {esc(item['title'] or identifier)}</h1>"
    body += "<p>La búsqueda, descarga y análisis se hacen juntos desde un único botón. Solo necesitas intervenir para confirmar resultados dudosos o registrar las obras identificadas.</p>"
    body += "<section class='panel'><h2>Proceso automático</h2><p>Busca archivos musicales en Internet Archive, descarga candidatos y analiza lo que se pueda leer.</p><form method='post' action='/items/" + esc(identifier) + "/analyze'><button>Buscar, descargar y analizar archivos</button></form></section>"
    if analizados is not None or omitidos is not None or fallidos is not None:
        body += (
            "<section class='panel'><h2>Resultado de la última ejecución</h2><p>"
            + "Analizados ahora: <b>" + esc(analizados or 0) + "</b> · "
            + "Omitidos por estar actualizados: <b>" + esc(omitidos or 0) + "</b> · "
            + "Fallidos: <b>" + esc(fallidos or 0) + "</b></p></section>"
        )
    body += "<section class='panel' id='clasificacion'><h2>Qué parece contener este material</h2><form method='post' action='/items/" + esc(identifier) + "/content-analysis/scope'><label>¿Qué parece contener? <select name='scope'>"
    for value, label in CONTENT_SCOPES.items():
        body += f"<option value='{value}' {'selected' if scope == value else ''}>{label}</option>"
    body += f"</select></label><p><label>Justificación / evidencias consultadas<br><textarea name='rationale' rows='3' style='width:95%'>{esc(rationale)}</textarea></label></p><button>Guardar clasificación</button></form></section>"
    body += "<section class='panel' id='archivos'><h2>Archivos encontrados</h2>"
    if not files:
        body += "<p>El inventario todavía no se ha consultado. El proceso automático de arriba lo consultará y buscará archivos candidatos.</p>"
    else:
        body += "<table><tr><th>Nombre</th><th>Formato</th><th>Tamaño</th><th>Candidato</th></tr>"
        for frow in files:
            body += f"<tr><td>{esc(frow['file_name'])}</td><td>{esc(frow['archive_format'] or '—')}</td><td>{esc(frow['size_bytes'] if frow['size_bytes'] is not None else '—')}</td><td>{'Sí' if frow['relevant'] else 'No'}</td></tr>"
        body += "</table>"
    body += "<p class='muted'>La lista de ficheros es una pista, no una prueba de cuántas obras contiene el elemento.</p></section>"
    analyses = []
    with db() as conn:
        analyses = conn.execute("""SELECT a.*, f.file_name FROM archive_file_analyses a
            JOIN archive_files f ON f.id=a.file_id
            WHERE a.identifier=? ORDER BY a.analyzed_at DESC,f.file_name""", (identifier,)).fetchall();
        pdf_extractions = conn.execute("""SELECT e.*, f.file_name FROM archive_pdf_extractions e
            JOIN archive_files f ON f.id=e.file_id
            WHERE e.identifier=? ORDER BY f.file_name""", (identifier,)).fetchall()
        downloaded_files = conn.execute("""SELECT file_name,archive_format,size_bytes FROM archive_files
            WHERE identifier=? AND download_status='downloaded' AND local_path IS NOT NULL
            ORDER BY file_name""", (identifier,)).fetchall()
    auto_analysis_suffixes = {".musicxml", ".mxl", ".xml", ".pdf", ".txt", ".ocr", ".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx", ".mid", ".midi", ".kar", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".jp2"}
    unparsed_files = [f for f in downloaded_files if Path(f["file_name"]).suffix.lower() not in auto_analysis_suffixes]
    body += "<section class='panel' id='analisis'><h2>Qué ha podido leer la aplicación</h2><p>Examina localmente los MusicXML/MXL y MIDI/KAR descargados. Los títulos explícitos de formatos editables se preparan automáticamente como obras candidatas; solo el texto/OCR dudoso queda para revisión. No se exporta nada a OSAP.</p><details><summary>Herramientas manuales de análisis (opcional)</summary><form method='post' action='/items/" + esc(identifier) + "/content-analysis/analyze-files'><button>Volver a comprobar los archivos</button></form></details>"
    if analyses:
        body += "<table><tr><th>Archivo</th><th>Estado</th><th>Título</th><th>Creadores</th><th>Partes</th><th>Compases</th><th>Analizado</th></tr>"
        for result in analyses:
            try:
                data = json.loads(result["analysis_json"])
            except (TypeError, json.JSONDecodeError):
                data = {}
            parts = data.get("parts", [])
            part_names = ", ".join(p.get("name") or p.get("id") or "Parte" for p in parts)
            creators = ", ".join(c.get("name", "") for c in data.get("creators", []) if c.get("name"))
            status_label = {
                "not_musicxml": "XML válido; no es una partitura MusicXML",
            }.get(result["status"], result["status"])
            body += f"<tr><td>{esc(result['file_name'])}</td><td>{esc(status_label)}{('<br>' + esc(result['error_text'])) if result['error_text'] else ''}</td><td>{esc(data.get('title') or '—')}</td><td>{esc(creators or '—')}</td><td>{esc(part_names or data.get('part_count', '—'))}</td><td>{esc(data.get('measure_count', '—'))}</td><td>{esc(result['analyzed_at'])}</td></tr>"
        body += "</table>"
    body += "</section>"
    body += "<section class='panel' id='ocr'><h2>Buscar títulos dentro de los archivos</h2><p>La aplicación lee TXT/OCR y formatos de notación compatibles, extrae texto de PDF y aplica OCR local a PDF escaneados e imágenes de partituras (PNG, JPG, TIFF y otros formatos compatibles). Las sugerencias de títulos detectados en texto normal requieren confirmación; los títulos explícitos de los formatos de notación se registran automáticamente como candidatos.</p>"
    extractable_suffixes = {".pdf", ".txt", ".ocr", ".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx",
                            ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".jp2"}
    if any(Path(row["file_name"]).suffix.casefold() in extractable_suffixes for row in downloaded_files):
        body += "<details><summary>Extracción manual de texto (opcional)</summary><form method='post' action='/items/" + esc(identifier) + "/content-analysis/extract'><button>Leer texto y buscar posibles títulos</button></form></details>"
    else:
        body += "<p>Aún no hay archivos descargados que este análisis pueda leer. El proceso automático de arriba buscará y analizará candidatos.</p>"
    if any(value is not None for value in (pdf_con_texto, pdf_ocr, pdf_ocr_realizados, pdf_ocr_sin_texto, errores_extraccion, sugerencias_nuevas, txt_leidos, notacion_leida, imagenes_ocr, imagenes_sin_texto, imagenes_leidas)):
        body += "<div class='notice'><h3>Resultado de la última extracción</h3><p>PDF con texto extraíble: " + esc(pdf_con_texto or 0) + " · PDF que aún necesitan OCR: " + esc(pdf_ocr or 0) + " · OCR realizado en PDF: " + esc(pdf_ocr_realizados or 0) + " · PDF sin texto legible: " + esc(pdf_ocr_sin_texto or 0) + " · Imágenes leídas por OCR: " + esc(imagenes_leidas or 0) + " · Imágenes con texto detectado: " + esc(imagenes_ocr or 0) + " · Imágenes sin texto legible: " + esc(imagenes_sin_texto or 0) + " · Ficheros TXT/OCR leídos: " + esc(txt_leidos or 0) + " · Errores de lectura: " + esc(errores_extraccion or 0) + " · Archivos ABC/LilyPond/MEI/MuseScore leídos: " + esc(notacion_leida or 0) + " · Sugerencias nuevas: " + esc(sugerencias_nuevas or 0) + "</p><p>Para que la revisión sea rápida, se extrae texto de un máximo de " + str(MAX_PDF_TEXT_PAGES) + " páginas por PDF y el OCR se limita a las primeras cinco páginas escaneadas. Las imágenes grandes se reducen antes de leerlas. Si Tesseract no está instalado o no está disponible, la ficha indica qué requisito falta. El procesamiento es local: los archivos no se envían a servicios externos.</p></div>";
    if pdf_extractions:
        body += "<h3>Estado de lectura de documentos e imágenes</h3><table><tr><th>Archivo</th><th>Resultado</th><th>Páginas</th><th>Caracteres extraídos</th><th>Último intento</th></tr>"
        pdf_status_labels = {"text_available": "Texto extraíble", "ocr_needed": "Necesita OCR", "ocr_completed": "OCR realizado", "ocr_no_text": "OCR realizado; sin texto legible", "image_ocr_completed": "Imagen leída por OCR", "image_ocr_no_text": "Imagen sin texto legible", "text_checked": "Lectura de texto realizada", "text_no_content": "Sin texto extraíble", "text_error": "Error al leer el archivo", "ocr_unavailable": "OCR no disponible", "ocr_error": "Error durante el OCR", "error": "Error de lectura"}
        for pdf_result in pdf_extractions:
            body += f"<tr><td>{esc(pdf_result['file_name'])}</td><td>{esc(pdf_status_labels.get(pdf_result['status'], pdf_result['status']))}{('<br>' + esc(pdf_result['error_text'])) if pdf_result['error_text'] else ''}</td><td>{esc(pdf_result['page_count'])}</td><td>{esc(pdf_result['extracted_characters'])}</td><td>{esc(pdf_result['analyzed_at'])}</td></tr>"
        body += "</table>"
    if unparsed_files:
        body += "<h3>Otros formatos descargados, pendientes de análisis automático</h3><p>Estos archivos están guardados localmente, pero esta versión todavía no extrae sus metadatos. Que aparezcan aquí no significa que sean inválidos.</p><table><tr><th>Archivo</th><th>Formato declarado</th><th>Tamaño</th></tr>"
        for other_file in unparsed_files:
            size_label = f"{other_file['size_bytes']:,} bytes" if other_file["size_bytes"] is not None else "—"
            body += f"<tr><td>{esc(other_file['file_name'])}</td><td>{esc(other_file['archive_format'] or 'No indicado')}</td><td>{esc(size_label)}</td></tr>"
        body += "</table>"
    with db() as conn:
        suggestions = conn.execute("SELECT * FROM archive_content_suggestions WHERE identifier=? AND status='pending' ORDER BY source_file,line_number,id", (identifier,)).fetchall()
    if suggestions:
        body += "<h3>Sugerencias para revisar</h3><table><tr><th>Texto detectado</th><th>Origen</th><th>Estado / acción</th></tr>"
        for suggestion in suggestions:
            body += f"<tr><td>{esc(suggestion['suggestion_text'])}</td><td>{esc(suggestion['source_file'])}<br>línea/página {suggestion['line_number']}</td><td>{esc(suggestion['status'])}"
            if suggestion["status"] == "pending":
                body += f"<form method='post' action='/items/{esc(identifier)}/content-analysis/suggestions/{suggestion['id']}/accept'><button>Aceptar como obra candidata</button></form>"
            body += "</td></tr>"
        body += "</table>"
    body += "</section>"
    body += "<section class='panel' id='obras'><h2><span class='step-number'>4</span>Obras o unidades de contenido identificadas</h2><p>Registra cada obra visible dentro de un PDF, volumen o conjunto. El fichero y las páginas son opcionales, pero ayudan a localizarla después.</p>"
    body += "<form method='post' action='/items/" + esc(identifier) + "/content-analysis/units'><p><label>Título de la obra o unidad *<br><input type='text' name='title' required></label></p><p><label>Compositor / creador<br><input type='text' name='creator'></label></p><p><label>Fichero de origen<br><input type='text' name='source_file'></label></p><p><label>Páginas / rango / localizador<br><input type='text' name='page_range' placeholder='p. 1–8, pistas 3–5, páginas del PDF'></label></p><p><label>Evidencia (índice, portada, contenido, OCR...)<br><input type='text' name='evidence' style='width:90%'></label></p><p><label>Notas<br><textarea name='notes' rows='2' style='width:95%'></textarea></label></p><button>Añadir obra candidata</button></form>"
    if units:
        body += "<h3>Contenido registrado</h3><table><tr><th>#</th><th>Título / creador</th><th>Localizador</th><th>Evidencia / notas</th><th>Estado</th></tr>"
        for unit in units:
            body += f"<tr><td>{unit['ordinal']}</td><td><b>{esc(unit['title'])}</b><br>{esc(unit['creator'] or 'Creador desconocido')}</td><td>{esc(unit['source_file'])}<br>{esc(unit['page_range'])}</td><td>{esc(unit['evidence'])}<br>{esc(unit['notes'])}</td><td>{esc(unit['status'])}</td></tr>"
        body += "</table>"
    else:
        body += "<p>Todavía no se han registrado obras candidatas. No se infiere automáticamente que el elemento equivalga a una sola obra.</p>"
    body += "<div class='section-nav'><a href='#archivos'>Volver a los archivos</a><a href='/items/" + esc(identifier) + "/content-analysis?step=5#obras'>Ver obras candidatas →</a></div></section>"
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
    """Return likely title lines, excluding OCR fragments, lyrics-like lines and credits."""
    ignored = (
        "internet archive", "table of contents", "contents", "[page ", "copyright",
        "all rights reserved", "public domain", "digitized by", "scanned by", "www.",
        "http://", "https://", "words and music by", "music and words by",
        "lyrics by", "music by", "arranged by", "written by", "published by",
        "printed in", "walt disney production", "hit selections from", "for sale",
        "price ", "copyrighted", "rights of public performance",
    )
    title_connectors = {
        "of", "the", "and", "in", "for", "from", "to", "a", "an", "de", "del",
        "la", "las", "y", "en", "el", "los", "una", "un", "minor", "major",
        "flat", "sharp", "op", "no",
    }
    candidates = []
    seen = set()
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = " ".join(raw.replace("\x0c", " ").split()).strip(" .·—-")
        if not 4 <= len(line) <= 140:
            continue
        low = line.casefold()
        if any(token in low for token in ignored):
            continue
        if re.match(r"^(?:col|capt|lt|maj|gen|mr|mrs|ms|dr)\.", low):
            continue
        if re.fullmatch(r"[\W_\d]+", line) or len(re.findall(r"\d", line)) > len(line) * 0.35:
            continue
        if len(line.split()) > 18 or line.endswith((":", ";", ",")):
            continue
        # Contents pages often end in dotted leaders and a page number.
        line = re.sub(r"\s*[.·_]{2,}\s*\d{1,4}\s*$", "", line).strip(" .·—-")
        line = re.sub(r"^\s*(?:no?\.?\s*)?\d{1,3}[.)]\s*", "", line).strip()
        if not 4 <= len(line) <= 140:
            continue
        letters = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]", line)
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", line)
        if len(letters) < 6 or not words:
            continue
        if sum(len(word) >= 5 for word in words) == 0 and len(letters) < 12:
            continue
        if len(letters) / max(1, len(re.sub(r"\s", "", line))) < 0.60:
            continue
        uppercase_ratio = sum(char.isupper() for char in letters) / len(letters)
        title_case_like = (
            len(words) <= 8
            and all(word[0].isupper() or word.casefold() in title_connectors for word in words)
        )
        # Music titles are often uppercase or title case; sentence-like lyric
        # lines and OCR gibberish are much less likely to be headings.
        if uppercase_ratio < 0.72 and not title_case_like:
            continue
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            candidates.append((line_no, line))
    return candidates[:300]

def extract_notation_title_candidates(text: str, suffix: str) -> list[tuple[int, str]]:
    """Extract explicit title metadata from text-based music notation formats."""
    import html

    found = []
    if suffix == ".abc":
        for line_no, raw in enumerate(text.splitlines(), 1):
            match = re.match(r"^\s*T\s*:\s*(.*?)\s*$", raw)
            if match:
                title = match.group(1).strip()
                if 4 <= len(title) <= 140:
                    found.append((line_no, title))
    elif suffix == ".ly":
        for line_no, raw in enumerate(text.splitlines(), 1):
            match = re.search(r'\btitle\s*=\s*"([^"]{4,140})"', raw)
            if match:
                found.append((line_no, match.group(1).strip()))
    elif suffix == ".mei":
        pattern = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?title\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?title\s*>", re.IGNORECASE | re.DOTALL)
        for match in pattern.finditer(text):
            title = re.sub(r"<[^>]+>", " ", match.group(1))
            title = " ".join(html.unescape(title).split())
            if 4 <= len(title) <= 140:
                line_no = text.count(chr(10), 0, match.start()) + 1
                found.append((line_no, title))
    elif suffix in {".mscx", ".capx"}:
        pattern = re.compile(r'<metaTag\s+name=["\'](?:workTitle|movementTitle|title)["\']\s*>(.*?)</metaTag\s*>', re.IGNORECASE | re.DOTALL)
        for match in pattern.finditer(text):
            title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())
            if 4 <= len(title) <= 140:
                line_no = text.count(chr(10), 0, match.start()) + 1
                found.append((line_no, title))
        if suffix == ".capx":
            title_pattern = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?title\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?title\s*>", re.IGNORECASE | re.DOTALL)
            for match in title_pattern.finditer(text):
                title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())
                if 4 <= len(title) <= 140:
                    line_no = text.count(chr(10), 0, match.start()) + 1
                    found.append((line_no, title))
    unique = []
    seen = set()
    for line_no, title in found:
        key = title.casefold()
        if key not in seen:
            seen.add(key)
            unique.append((line_no, title))
    return unique[:100]


def register_auto_work_candidate(
    identifier: str, title: str, creator: str = "", source_file: str = "", evidence: str = ""
) -> bool:
    """Register a provisional local candidate from explicit machine-readable metadata."""
    title = " ".join((title or "").split()).strip()
    creator = " ".join((creator or "").split()).strip()
    if not 4 <= len(title) <= 1000:
        return False
    with db() as conn:
        existing = conn.execute(
            "SELECT id,creator FROM archive_item_contents WHERE identifier=? AND lower(title)=lower(?) LIMIT 1",
            (identifier, title),
        ).fetchone()
        if existing:
            if creator and not existing["creator"]:
                conn.execute(
                    "UPDATE archive_item_contents SET creator=? WHERE id=?",
                    (creator[:500], existing["id"]),
                )
            return False
        ordinal = int(conn.execute(
            "SELECT COALESCE(MAX(ordinal),0)+1 FROM archive_item_contents WHERE identifier=?",
            (identifier,),
        ).fetchone()[0])
        conn.execute(
            """INSERT INTO archive_item_contents
               (identifier,ordinal,title,creator,source_file,page_range,evidence,status,notes,created_at)
               VALUES(?,?,?,?,?,'',?,'candidate',?,?)""",
            (
                identifier, ordinal, title, creator[:500], source_file[:1000],
                evidence[:2000] or "Título explícito de metadatos del archivo.",
                "Candidato creado automáticamente desde metadatos explícitos; aún no exportado a OSAP.",
                now(),
            ),
        )
    return True


@app.post("/items/{identifier}/content-analysis/analyze-files")
def analyze_downloaded_musicxml(identifier: str):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM archive_items WHERE identifier=?", (identifier,)).fetchone():
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        files = conn.execute("""SELECT id,file_name,local_path,sha256 FROM archive_files
            WHERE identifier=? AND download_status='downloaded' AND local_path IS NOT NULL
            AND (lower(file_name) LIKE '%.musicxml'
                 OR lower(file_name) LIKE '%.mxl'
                 OR lower(file_name) LIKE '%.xml'
                 OR lower(file_name) LIKE '%.mid'
                 OR lower(file_name) LIKE '%.midi'
                 OR lower(file_name) LIKE '%.kar')""", (identifier,)).fetchall()
    if not files:
        raise HTTPException(status_code=422, detail="No hay archivos MusicXML/MXL o MIDI descargados para analizar.")
    analyzed_count = 0
    skipped_count = 0
    failed_count = 0
    for file_row in files:
        # Do not repeat successful analysis when the downloaded content is unchanged.
        # Failed analyses remain eligible for retry, and files without a digest are
        # deliberately rechecked because their identity cannot be verified safely.
        with db() as conn:
            previous = conn.execute(
                "SELECT status,sha256,analysis_json FROM archive_file_analyses WHERE file_id=?",
                (file_row["id"],),
            ).fetchone()
        if (
            previous
            and previous["status"] in {"ok", "not_musicxml"}
            and file_row["sha256"]
            and previous["sha256"] == file_row["sha256"]
        ):
            try:
                previous_analysis = json.loads(previous["analysis_json"] or "{}")
                if previous_analysis.get("title"):
                    creators = previous_analysis.get("creators") or []
                    creator = creators[0].get("name", "") if creators and isinstance(creators[0], dict) else ""
                    register_auto_work_candidate(
                        identifier, previous_analysis["title"], creator, file_row["file_name"],
                        "Título explícito de metadatos MusicXML/MXL; análisis anterior sin cambios.",
                    )
            except (TypeError, json.JSONDecodeError, AttributeError):
                pass
            skipped_count += 1
            continue

        path = Path(file_row["local_path"])
        status = "ok"
        error_text = None
        try:
            if not path.is_file():
                raise FileNotFoundError("El archivo descargado no existe en la ruta local registrada.")
            if path.suffix.lower() in {".mid", ".midi", ".kar"}:
                analysis = analyze_midi(path)
            else:
                analysis = analyze_musicxml(path)
        except NotMusicXMLFileError as exc:
            if path.suffix.lower() != ".xml":
                analysis = {"format": path.suffix.lower().lstrip("."), "status": "error"}
                status = "error"
                error_text = str(exc)[:1000]
            else:
                analysis = {
                    "format": "xml",
                    "status": "not_musicxml",
                    "message": "XML válido, pero no es una partitura MusicXML.",
                }
                status = "not_musicxml"
        except Exception as exc:
            analysis = {"format": path.suffix.lower().lstrip("."), "status": "error"}
            status = "error"
            error_text = str(exc)[:1000]
        analyzed_count += 1
        if status == "error":
            failed_count += 1
        if status == "ok" and analysis.get("title"):
            creators = analysis.get("creators") or []
            creator = creators[0].get("name", "") if creators and isinstance(creators[0], dict) else ""
            register_auto_work_candidate(
                identifier, analysis["title"], creator, file_row["file_name"],
                "Título explícito de metadatos MusicXML/MXL; estructura analizada localmente.",
            )
        with db() as conn:
            conn.execute("""INSERT INTO archive_file_analyses
                (file_id,identifier,analysis_type,status,analysis_json,error_text,analyzed_at,sha256)
                VALUES(?,?, ?, ?,?,?,?,?)
                ON CONFLICT(file_id) DO UPDATE SET identifier=excluded.identifier,
                analysis_type=excluded.analysis_type,status=excluded.status,
                analysis_json=excluded.analysis_json,error_text=excluded.error_text,
                analyzed_at=excluded.analyzed_at,sha256=excluded.sha256""",
                (file_row["id"], identifier,
                 "midi" if path.suffix.lower() in {".mid", ".midi", ".kar"} else "musicxml",
                 status, json.dumps(analysis, ensure_ascii=False), error_text, now(), file_row["sha256"]))
    return RedirectResponse(
        url=(
            "/items/" + identifier + "/content-analysis"
            + "?analizados=" + str(analyzed_count)
            + "&omitidos=" + str(skipped_count)
            + "&fallidos=" + str(failed_count)
        ),
        status_code=303,
    )


@app.post("/items/{identifier}/content-analysis/extract")
def extract_content_suggestions(identifier: str):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM archive_items WHERE identifier=?", (identifier,)).fetchone():
            raise HTTPException(status_code=404, detail="Elemento no encontrado")
        files = conn.execute("""SELECT id,file_name,local_path,sha256 FROM archive_files
            WHERE identifier=? AND download_status='downloaded' AND local_path IS NOT NULL""", (identifier,)).fetchall()
    if not files:
        raise HTTPException(status_code=422, detail="Primero consulta el inventario y descarga un archivo compatible: texto, PDF, imagen o formato de notación.")

    def record_pdf_result(file_row, status, page_count=0, extracted_characters=0, error_text=None):
        with db() as conn:
            conn.execute("""INSERT INTO archive_pdf_extractions
                (file_id,identifier,status,page_count,extracted_characters,error_text,analyzed_at,sha256)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(file_id) DO UPDATE SET identifier=excluded.identifier,status=excluded.status,
                page_count=excluded.page_count,extracted_characters=excluded.extracted_characters,
                error_text=excluded.error_text,analyzed_at=excluded.analyzed_at,sha256=excluded.sha256""",
                (file_row["id"], identifier, status, page_count, extracted_characters,
                 error_text, now(), file_row["sha256"]))

    pdf_with_text = 0
    pdf_needs_ocr = 0
    pdf_ocr_completed = 0
    pdf_ocr_no_text = 0
    image_ocr_completed = 0
    image_ocr_no_text = 0
    images_read = 0
    extraction_errors = 0
    text_files_read = 0
    notation_files_read = 0
    inserted = 0
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".jp2"}
    for frow in files:
        path = Path(frow["local_path"])
        suffix = path.suffix.lower()
        text_suffixes = {".txt", ".ocr", ".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx"}
        if suffix not in ({".pdf"} | text_suffixes | image_suffixes):
            continue
        if not path.is_file():
            extraction_errors += 1
            if suffix == ".pdf":
                record_pdf_result(frow, "error", error_text="No existe el fichero en la ruta local registrada.")
            elif suffix in image_suffixes:
                record_pdf_result(frow, "ocr_error", error_text="No existe el fichero en la ruta local registrada.")
            elif suffix in text_suffixes:
                record_pdf_result(frow, "text_error", error_text="No existe el fichero en la ruta local registrada.")
            continue
        text_content = ""
        if (suffix == ".pdf" or suffix in image_suffixes or suffix in text_suffixes) and frow["sha256"]:
            with db() as conn:
                previous_pdf = conn.execute(
                    "SELECT status,sha256 FROM archive_pdf_extractions WHERE file_id=?",
                    (frow["id"],),
                ).fetchone()
            if (
                previous_pdf
                and previous_pdf["sha256"] == frow["sha256"]
                and previous_pdf["status"] in {"text_available", "ocr_completed", "ocr_no_text", "image_ocr_completed", "image_ocr_no_text", "text_checked", "text_no_content"}
            ):
                continue
        try:
            if suffix in {".txt", ".ocr", ".abc", ".ly", ".mei", ".mscx"}:
                text_content = path.read_text(encoding="utf-8-sig", errors="replace")
                if text_content.strip():
                    if suffix in {".abc", ".ly", ".mei", ".mscx", ".capx"}:
                        notation_files_read += 1
                    else:
                        text_files_read += 1
            elif suffix in {".mscz", ".capx"}:
                import zipfile
                member_limit = 20 * 1024 * 1024
                with zipfile.ZipFile(path) as archive:
                    candidates = [
                        info for info in archive.infolist()
                        if (
                            info.filename.lower().endswith(".mscx") if suffix == ".mscz"
                            else info.filename.lower().endswith((".xml", ".capella", ".capx"))
                        )
                        and not info.filename.startswith("/")
                        and ".." not in Path(info.filename).parts
                    ]
                    if not candidates:
                        expected = "un archivo MSCX" if suffix == ".mscz" else "un XML de partitura reconocible"
                        raise ValueError("El paquete no contiene " + expected + ".")
                    member = candidates[0]
                    if member.file_size > member_limit:
                        raise ValueError("El archivo MSCX interno supera el límite de 20 MiB.")
                    with archive.open(member) as source:
                        payload = source.read(member_limit + 1)
                    if len(payload) > member_limit:
                        raise ValueError("El archivo MSCX interno supera el límite de 20 MiB.")
                    text_content = payload.decode("utf-8-sig", errors="replace")
                if text_content.strip():
                    notation_files_read += 1
            elif suffix in image_suffixes:
                images_read += 1
                try:
                    import pytesseract
                    from PIL import Image, ImageOps
                except ImportError as dependency_error:
                    extraction_errors += 1
                    record_pdf_result(frow, "ocr_unavailable", error_text="OCR de imágenes no disponible: instala Pillow y pytesseract.")
                    continue
                try:
                    try:
                        pytesseract.get_tesseract_version()
                    except Exception as availability_error:
                        extraction_errors += 1
                        record_pdf_result(frow, "ocr_unavailable", error_text="OCR de imágenes no disponible: instala Tesseract y comprueba que esté en PATH.")
                        continue
                    with Image.open(path) as source_image:
                        image = ImageOps.exif_transpose(source_image)
                        image.thumbnail((2500, 2500))
                        if image.mode not in {"RGB", "L"}:
                            image = image.convert("RGB")
                        ocr_language = os.getenv("AI_IMPORTACION_OCR_LANG", "eng").strip() or "eng"
                        text_content = pytesseract.image_to_string(image, lang=ocr_language) or ""
                    extracted_characters = len(text_content)
                    if text_content.strip():
                        image_ocr_completed += 1
                        record_pdf_result(frow, "image_ocr_completed", 1, extracted_characters)
                    else:
                        image_ocr_no_text += 1
                        record_pdf_result(frow, "image_ocr_no_text", 1, 0, "El OCR no encontró texto legible en la imagen.")
                except Exception as image_error:
                    extraction_errors += 1
                    record_pdf_result(frow, "ocr_error", error_text=str(image_error)[:1000])
                    continue
            elif suffix == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(str(path))
                pages = []
                page_texts = []
                page_count = len(reader.pages)
                page_limit = min(page_count, MAX_PDF_TEXT_PAGES)
                for page_no in range(page_limit):
                    pdf_page = reader.pages[page_no]
                    page_text = pdf_page.extract_text() or ""
                    page_texts.append(page_text)
                    pages.append(chr(10) + f"[PAGE {page_no + 1}]" + chr(10) + page_text)
                text_content = chr(10).join(pages)
                extracted_characters = sum(len(page_text) for page_text in page_texts)
                if any(page_text.strip() for page_text in page_texts):
                    pdf_with_text += 1
                    record_pdf_result(
                        frow, "text_available", page_count, extracted_characters,
                        f"Texto extraído de las primeras {page_limit} páginas."
                        if page_count > page_limit else None,
                    )
                else:
                    # Raster PDFs need OCR; process only a small, bounded prefix locally.
                    class OCRUnavailableError(RuntimeError):
                        pass

                    try:
                        import io
                        try:
                            import fitz
                            import pytesseract
                            from PIL import Image
                        except ImportError as dependency_error:
                            raise OCRUnavailableError(
                                "OCR local no disponible: instala las dependencias Python del proyecto."
                            ) from dependency_error

                        try:
                            pytesseract.get_tesseract_version()
                        except Exception as availability_error:
                            raise OCRUnavailableError(
                                "OCR local no disponible: instala Tesseract y comprueba que esté en PATH."
                            ) from availability_error

                        ocr_pages = []
                        with fitz.open(str(path)) as document:
                            for page_index in range(min(len(reader.pages), 5)):
                                page = document[page_index]
                                page_dimension = max(
                                    float(page.rect.width), float(page.rect.height), 1.0
                                )
                                scale = min(2.0, 2500.0 / page_dimension)
                                pixmap = page.get_pixmap(
                                    matrix=fitz.Matrix(scale, scale), alpha=False
                                )
                                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                                ocr_language = os.getenv("AI_IMPORTACION_OCR_LANG", "eng").strip() or "eng"
                                recognized = pytesseract.image_to_string(image, lang=ocr_language)
                                ocr_pages.append(recognized or "")
                        text_content = chr(10).join(
                            chr(10) + f"[PAGE {page_index + 1} OCR]" + chr(10) + page_text
                            for page_index, page_text in enumerate(ocr_pages)
                        )
                        extracted_characters = sum(len(page_text) for page_text in ocr_pages)
                        if any(page_text.strip() for page_text in ocr_pages):
                            pdf_ocr_completed += 1
                            record_pdf_result(
                                frow, "ocr_completed", len(reader.pages), extracted_characters,
                                "OCR aplicado a las primeras cinco páginas como máximo."
                                if len(reader.pages) > 5 else None,
                            )
                        else:
                            pdf_ocr_no_text += 1
                            record_pdf_result(
                                frow, "ocr_no_text", len(reader.pages), extracted_characters,
                                "El OCR terminó, pero no encontró texto legible en las páginas procesadas.",
                            )
                    except OCRUnavailableError as ocr_unavailable:
                        pdf_needs_ocr += 1
                        record_pdf_result(
                            frow, "ocr_unavailable", len(reader.pages), 0, str(ocr_unavailable)
                        )
                    except Exception as ocr_error:
                        extraction_errors += 1
                        record_pdf_result(
                            frow, "ocr_error", len(reader.pages), 0, str(ocr_error)[:1000]
                        )
        except Exception as exc:
            extraction_errors += 1
            if suffix == ".pdf":
                record_pdf_result(frow, "error", error_text=str(exc)[:1000])
            elif suffix in image_suffixes:
                record_pdf_result(frow, "ocr_error", error_text=str(exc)[:1000])
            elif suffix in text_suffixes:
                record_pdf_result(frow, "text_error", error_text=str(exc)[:1000])
            continue
        if suffix in text_suffixes:
            record_pdf_result(
                frow,
                "text_checked" if text_content.strip() else "text_no_content",
                0,
                len(text_content),
                None if text_content.strip() else "La lectura terminó sin texto extraíble.",
            )
        if not text_content.strip():
            continue
        candidate_rows = (
            extract_notation_title_candidates(text_content, ".mscx" if suffix == ".mscz" else suffix)
            if suffix in {".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx"}
            else extract_text_candidates(text_content)
        )
        structured_title_source = suffix in {".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx"}
        auto_titles = []
        candidate_keys = {(line_no, candidate) for line_no, candidate in candidate_rows}
        with db() as conn:
            # Reprocessing may improve OCR filtering. Keep old decisions auditable,
            # but stop showing stale pending fragments that no longer pass the filter.
            previous_pending = conn.execute(
                """SELECT id,line_number,suggestion_text FROM archive_content_suggestions
                   WHERE identifier=? AND source_file=? AND status='pending'""",
                (identifier, frow["file_name"]),
            ).fetchall()
            for previous_suggestion in previous_pending:
                if (previous_suggestion["line_number"], previous_suggestion["suggestion_text"]) not in candidate_keys:
                    conn.execute(
                        "UPDATE archive_content_suggestions SET status='filtered_noise' WHERE id=?",
                        (previous_suggestion["id"],),
                    )
            for line_no, candidate in candidate_rows:
                cur = conn.execute("""INSERT OR IGNORE INTO archive_content_suggestions
                    (identifier,source_file,line_number,suggestion_text,status,created_at)
                    VALUES(?,?,?,?, 'pending', ?)""",
                    (identifier, frow["file_name"], line_no, candidate, now()))
                inserted += cur.rowcount
                if structured_title_source:
                    suggestion = conn.execute(
                        """SELECT id,status FROM archive_content_suggestions
                           WHERE identifier=? AND source_file=? AND line_number=? AND suggestion_text=?""",
                        (identifier, frow["file_name"], line_no, candidate),
                    ).fetchone()
                    if suggestion and suggestion["status"] == "pending":
                        conn.execute(
                            "UPDATE archive_content_suggestions SET status='auto_accepted' WHERE id=?",
                            (suggestion["id"],),
                        )
                        auto_titles.append(candidate)
        for candidate in auto_titles:
            register_auto_work_candidate(
                identifier, candidate, source_file=frow["file_name"],
                evidence=f"Título explícito extraído de metadatos del formato {suffix}.",
            )

    return RedirectResponse(
        url=(
            "/items/" + identifier + "/content-analysis"
            + "?pdf_con_texto=" + str(pdf_with_text)
            + "&pdf_ocr=" + str(pdf_needs_ocr)
            + "&pdf_ocr_realizados=" + str(pdf_ocr_completed)
            + "&pdf_ocr_sin_texto=" + str(pdf_ocr_no_text)
            + "&txt_leidos=" + str(text_files_read)
            + "&notacion_leida=" + str(notation_files_read)
            + "&imagenes_ocr=" + str(image_ocr_completed)
            + "&imagenes_sin_texto=" + str(image_ocr_no_text)
            + "&imagenes_leidas=" + str(images_read)
            + "&errores_extraccion=" + str(extraction_errors)
            + "&sugerencias_nuevas=" + str(inserted)
        ),
        status_code=303,
    )

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



@app.get("/intake/analysis-status/{job_id}", response_class=HTMLResponse)
def content_analysis_job_status(
    job_id: int, descargados: int | None = None, archivos: int | None = None
):
    with db() as conn:
        job = conn.execute("SELECT * FROM content_analysis_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo de análisis no encontrado")
    active = job["status"] in {"pending", "running"}
    refresh = "<meta http-equiv='refresh' content='3'>" if active else ""
    body = "<h1>Estado del análisis automático</h1>"
    if descargados is not None and archivos is not None:
        body += (
            "<p>Archivos candidatos descargados en esta ejecución: <b>"
            + esc(descargados)
            + "</b>. Archivos candidatos guardados para este elemento: <b>"
            + esc(archivos)
            + "</b>.</p>"
        )
    if job["status"] == "pending":
        body += "<p>El análisis está en cola y comenzará cuando el trabajador esté disponible.</p>"
    elif job["status"] == "running":
        body += "<p>Se están analizando los archivos en segundo plano. Puedes salir de esta página; el resultado se conserva en el catálogo.</p>"
    elif job["status"] == "completed":
        body += "<p>Análisis terminado. Consulta las fichas de los elementos y la cola de revisión para ver los resultados.</p>"
    else:
        body += "<p>El análisis terminó con un error. Puedes volver a encolarlo desde Captación integrada.</p>"
    if job["error_text"]:
        body += "<details class='panel'><summary>Detalle del error</summary><pre>" + esc(job["error_text"]) + "</pre></details>"
    if job["identifier"]:
        body += "<p><a href='/items/" + quote(job["identifier"], safe="") + "/content-analysis'>Ver análisis del elemento</a> · <a href='/items/" + quote(job["identifier"], safe="") + "'>Volver a su ficha</a></p>"
    body += "<p><a href='/review'>Revisar resultados</a> · <a href='/intake'>Captación integrada</a> · <a href='/'>Inicio</a></p>"
    rendered = page("Análisis automático", body).body.decode("utf-8")
    if active:
        rendered = rendered.replace("<meta charset='utf-8'>", "<meta charset='utf-8'>" + refresh, 1)
    return HTMLResponse(rendered)


@app.post("/intake/process", response_class=HTMLResponse)
def process_downloaded_batch(identifier: str | None = None):
    """Analyze a bounded batch of already downloaded files without per-file user actions."""
    with db() as conn:
        targets = conn.execute(
            """SELECT a.identifier,COALESCE(a.title,a.identifier) AS title
               FROM archive_items a
               WHERE (? IS NULL OR a.identifier=?)
                 AND EXISTS (
                 SELECT 1 FROM archive_files f
                 WHERE f.identifier=a.identifier AND f.download_status='downloaded'
                   AND (
                     lower(f.file_name) LIKE '%.txt' OR lower(f.file_name) LIKE '%.ocr'
                     OR lower(f.file_name) LIKE '%.pdf' OR lower(f.file_name) LIKE '%.abc'
                     OR lower(f.file_name) LIKE '%.ly' OR lower(f.file_name) LIKE '%.mei'
                     OR lower(f.file_name) LIKE '%.mscx' OR lower(f.file_name) LIKE '%.mscz'
                     OR lower(f.file_name) LIKE '%.capx' OR lower(f.file_name) LIKE '%.png'
                     OR lower(f.file_name) LIKE '%.jpg' OR lower(f.file_name) LIKE '%.jpeg'
                     OR lower(f.file_name) LIKE '%.webp' OR lower(f.file_name) LIKE '%.tif'
                     OR lower(f.file_name) LIKE '%.tiff' OR lower(f.file_name) LIKE '%.bmp'
                     OR lower(f.file_name) LIKE '%.jp2' OR lower(f.file_name) LIKE '%.musicxml'
                     OR lower(f.file_name) LIKE '%.mxl' OR lower(f.file_name) LIKE '%.xml'
                     OR lower(f.file_name) LIKE '%.mid' OR lower(f.file_name) LIKE '%.midi'
                     OR lower(f.file_name) LIKE '%.kar'
                   )
               ) AND EXISTS (
                 SELECT 1 FROM archive_files f
                 WHERE f.identifier=a.identifier AND f.download_status='downloaded'
                   AND (
                     (lower(f.file_name) LIKE '%.txt' OR lower(f.file_name) LIKE '%.ocr'
                      OR lower(f.file_name) LIKE '%.pdf' OR lower(f.file_name) LIKE '%.abc'
                      OR lower(f.file_name) LIKE '%.ly' OR lower(f.file_name) LIKE '%.mei'
                      OR lower(f.file_name) LIKE '%.mscx' OR lower(f.file_name) LIKE '%.mscz'
                      OR lower(f.file_name) LIKE '%.capx' OR lower(f.file_name) LIKE '%.png'
                      OR lower(f.file_name) LIKE '%.jpg' OR lower(f.file_name) LIKE '%.jpeg'
                      OR lower(f.file_name) LIKE '%.webp' OR lower(f.file_name) LIKE '%.tif'
                      OR lower(f.file_name) LIKE '%.tiff' OR lower(f.file_name) LIKE '%.bmp'
                      OR lower(f.file_name) LIKE '%.jp2')
                     AND NOT EXISTS (
                       SELECT 1 FROM archive_pdf_extractions pe
                       WHERE pe.file_id=f.id AND pe.sha256=f.sha256
                     )
                     OR
                     (lower(f.file_name) LIKE '%.musicxml' OR lower(f.file_name) LIKE '%.mxl'
                      OR lower(f.file_name) LIKE '%.xml' OR lower(f.file_name) LIKE '%.mid'
                      OR lower(f.file_name) LIKE '%.midi' OR lower(f.file_name) LIKE '%.kar')
                     AND NOT EXISTS (
                       SELECT 1 FROM archive_file_analyses fa
                       WHERE fa.file_id=f.id AND fa.sha256=f.sha256
                     )
                   )
               )
               ORDER BY (
                 SELECT MAX(f2.id) FROM archive_files f2
                 WHERE f2.identifier=a.identifier AND f2.download_status='downloaded'
               ) DESC LIMIT 10""",
            (identifier, identifier),
        ).fetchall()

    results = []
    for target in targets:
        identifier = target["identifier"]
        title = target["title"]
        with db() as conn:
            file_names = [
                row["file_name"].lower()
                for row in conn.execute(
                    "SELECT file_name FROM archive_files WHERE identifier=? AND download_status='downloaded'",
                    (identifier,),
                ).fetchall()
            ]
            before_suggestions = conn.execute(
                "SELECT COUNT(*) FROM archive_content_suggestions WHERE identifier=?", (identifier,)
            ).fetchone()[0]
        extraction_suffixes = (".txt", ".ocr", ".pdf", ".abc", ".ly", ".mei", ".mscx", ".mscz", ".capx", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".jp2")
        musicxml_suffixes = (".musicxml", ".mxl", ".xml", ".mid", ".midi", ".kar")
        errors = []
        try:
            if any(name.endswith(extraction_suffixes) for name in file_names):
                extract_content_suggestions(identifier)
        except Exception as exc:
            errors.append("Texto/partitura: " + str(exc)[:180])
        try:
            if any(name.endswith(musicxml_suffixes) for name in file_names):
                analyze_downloaded_musicxml(identifier)
        except Exception as exc:
            errors.append("Análisis MusicXML: " + str(exc)[:180])
        with db() as conn:
            after_suggestions = conn.execute(
                "SELECT COUNT(*) FROM archive_content_suggestions WHERE identifier=?", (identifier,)
            ).fetchone()[0]
            analyses = conn.execute(
                "SELECT COUNT(*) FROM archive_file_analyses WHERE identifier=? AND status='ok'",
                (identifier,),
            ).fetchone()[0]
        if any(name.endswith(extraction_suffixes + musicxml_suffixes) for name in file_names):
            results.append({
                "identifier": identifier,
                "title": title,
                "new_suggestions": max(0, after_suggestions - before_suggestions),
                "analyses": analyses,
                "errors": errors,
            })

    body = (
        "<!doctype html><html lang='es'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Procesamiento automático</title>"
        "<body style='font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem'>"
        "<h1>Resultado del análisis automático</h1>"
        "<p>Se han procesado como máximo 10 elementos con archivos descargados. "
        "La aplicación ha extraído indicios disponibles sin aceptar obras ni exportar nada a OSAP.</p>"
    )
    if not results:
        body += "<p>No hay archivos descargados compatibles con los analizadores actuales. "
        "Primero ejecuta la captación integrada para inventariar y descargar material.</p>"
    else:
        body += "<table><tr><th>Elemento</th><th>Nuevas sugerencias</th><th>Análisis estructurales correctos</th><th>Incidencias</th></tr>"
        for result in results:
            link = "/items/" + quote(result["identifier"], safe="") + "/content-analysis"
            errors = "; ".join(result["errors"]) if result["errors"] else "Ninguna"
            body += (
                "<tr><td><a href='" + esc(link) + "'>" + esc(result["title"]) + "</a></td>"
                "<td>" + str(result["new_suggestions"]) + "</td>"
                "<td>" + str(result["analyses"]) + "</td>"
                "<td>" + esc(errors) + "</td></tr>"
            )
        body += "</table>"
    body += (
        "<p>Las sugerencias son indicios; no se han convertido en obras automáticamente. "
        "Los formatos sin analizador permanecen pendientes.</p>"
        "<p><a href='/intake'>Volver a captación</a> · <a href='/'>Inicio</a></p></body></html>"
    )
    return HTMLResponse(body)
