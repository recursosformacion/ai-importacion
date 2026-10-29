from __future__ import annotations

import html
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
DOWNLOAD_DIR = Path(os.getenv("AI_IMPORTACION_DOWNLOAD_DIR", "downloads"))
MAX_DOWNLOAD_BYTES = int(os.getenv("AI_IMPORTACION_MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024)))
USER_AGENT = "OSAP-AI-Importacion/0.2 (+https://openmusicrepository.com)"
METADATA_ENDPOINT = "https://archive.org/metadata/"
ARCHIVE_ENDPOINT = "https://archive.org/advancedsearch.php"
SCRAPE_ENDPOINT = "https://archive.org/services/search/v1/scrape"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value: object | None) -> str:
    return html.escape("" if value is None else str(value))


def connect(db_path: str):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
