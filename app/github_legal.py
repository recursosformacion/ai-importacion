from __future__ import annotations

"""Repository-level legal admission, immutable evidence snapshots and audits."""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from app.db import DOWNLOAD_DIR, connect

DB_PATH = os.getenv("AI_IMPORTACION_DB", "data/ai-importacion.sqlite3")
API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
USER_AGENT = "OSAP-AI-Importacion/0.1 (+https://openmusicrepository.com)"
# These licenses permit redistribution and adaptation, subject to their terms.
OPEN_LICENSES = {
    "MIT", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "BSD-3-CLAUSE-CLEAR",
    "APACHE-2.0", "GPL-2.0", "GPL-2.0-ONLY", "GPL-2.0-OR-LATER",
    "GPL-3.0", "GPL-3.0-ONLY", "GPL-3.0-OR-LATER",
    "LGPL-2.0", "LGPL-2.1", "LGPL-3.0", "MPL-2.0",
    "CC0-1.0", "CC-BY-2.0", "CC-BY-2.5", "CC-BY-3.0", "CC-BY-4.0",
    "CC-BY-SA-2.0", "CC-BY-SA-2.5", "CC-BY-SA-3.0", "CC-BY-SA-4.0",
}
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md")
README_FILES = ("README.md", "README", "README.rst", "README.txt")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _headers():
    result = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
              "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        result["Authorization"] = "Bearer " + TOKEN
    return result


async def _get_json(client, url):
    response = await client.get(url)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Respuesta GitHub inesperada")
    return data


async def _get_text(client, full_name, path, commit):
    url = "https://raw.githubusercontent.com/" + full_name + "/" + commit + "/" + quote(path, safe="/")
    response = await client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def _license_id(text, api_spdx):
    body = (text or "").casefold()
    if not body:
        return None
    if "creative commons" in body or "creativecommons.org" in body:
        if re.search(r"attribution[- ]sharealike|by-sa", body):
            match = re.search(r"by-sa/(2\.0|2\.5|3\.0|4\.0)", body)
            return "CC-BY-SA-" + match.group(1) if match else None
        if re.search(r"attribution|cc by", body):
            match = re.search(r"(?:licenses/by/|cc by |attribution[^0-9]{0,30})(2\.0|2\.5|3\.0|4\.0)", body)
            return "CC-BY-" + match.group(1) if match else None
        if "cc0" in body or "public domain" in body:
            return "CC0-1.0"
        return None
    if "apache license" in body and "version 2.0" in body:
        return "APACHE-2.0"
    if "permission is hereby granted, free of charge" in body:
        return "MIT"
    if "redistribution and use in source and binary forms" in body:
        if "neither the name" in body:
            return "BSD-3-CLAUSE"
        return "BSD-2-CLAUSE"
    if "gnu general public license" in body and "version 3" in body:
        return "GPL-3.0"
    if "gnu general public license" in body and "version 2" in body:
        return "GPL-2.0"
    if "mozilla public license" in body and "2.0" in body:
        return "MPL-2.0"
    if "gnu lesser general public license" in body:
        if "version 3" in body:
            return "LGPL-3.0"
        if "version 2.1" in body:
            return "LGPL-2.1"
        if "version 2" in body:
            return "LGPL-2.0"
    return None


def _has_scope_exclusions(readme):
    text = (readme or "").casefold()
    patterns = (
        r"(?:scores|music|audio|media|assets|data|content|partituras).{0,100}"
        r"(?:not covered|excluded|all rights reserved|separate license|different license)",
        r"(?:not covered|excluded|all rights reserved|separate license).{0,100}"
        r"(?:scores|music|audio|media|assets|data|content|partituras)",
    )
    if any(re.search(pattern, text, re.S) for pattern in patterns):
        return True
    # A README may grant a different, restrictive license specifically to scores.
    for line in text.splitlines():
        if re.search(r"score|partitur|music|audio|mxl|musicxml", line) and re.search(
            r"cc[- ]?by[- ]?(?:nc|nd)|noncommercial|non-commercial|no derivatives|all rights reserved|\bproprietary\b",
            line,
        ):
            return True
    return False


def ensure_schema():
    # Back up the complete existing SQLite database before the first legal
    # schema migration. If the backup fails, abort before changing anything.
    database_path = Path(DB_PATH)
    if database_path.is_file():
        needs_backup = False
        with sqlite3.connect(str(database_path)) as probe:
            tables = {
                row[0] for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "github_repositories" in tables:
                columns = {
                    row[1] for row in probe.execute("PRAGMA table_info(github_repositories)")
                }
                required = {
                    "legal_status", "legal_license_spdx", "legal_license_path",
                    "legal_license_sha256", "legal_commit_sha", "legal_captured_at",
                    "legal_owner_login", "legal_owner_credit", "legal_alert",
                    "last_legal_audit_at",
                }
                needs_backup = (
                    not required.issubset(columns)
                    or "github_legal_snapshots" not in tables
                    or "github_legal_audits" not in tables
                )
        if needs_backup:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = database_path.with_name(
                database_path.name + ".pre-github-legal-migration-" + stamp + ".bak"
            )
            database_path.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(str(database_path))
            backup = sqlite3.connect(str(backup_path))
            try:
                source.backup(backup)
            finally:
                backup.close()
                source.close()
    with connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS github_legal_snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repository_id INTEGER NOT NULL REFERENCES github_repositories(id) ON DELETE CASCADE,
          commit_sha TEXT NOT NULL, captured_at TEXT NOT NULL,
          license_path TEXT NOT NULL, license_text TEXT NOT NULL,
          license_sha256 TEXT NOT NULL, license_spdx TEXT NOT NULL,
          readme_path TEXT, readme_text TEXT NOT NULL DEFAULT '',
          readme_sha256 TEXT NOT NULL DEFAULT '',
          repository_metadata_json TEXT NOT NULL,
          owner_login TEXT NOT NULL, owner_name TEXT NOT NULL DEFAULT '',
          author_credit TEXT NOT NULL DEFAULT '', license_url TEXT NOT NULL DEFAULT '',
          decision TEXT NOT NULL, decision_reason TEXT NOT NULL,
          UNIQUE(repository_id,commit_sha,license_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_github_legal_snapshots_repo
          ON github_legal_snapshots(repository_id,captured_at DESC);
        CREATE TABLE IF NOT EXISTS github_legal_audits(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repository_id INTEGER NOT NULL REFERENCES github_repositories(id) ON DELETE CASCADE,
          audited_at TEXT NOT NULL, current_commit_sha TEXT NOT NULL DEFAULT '',
          current_license_sha256 TEXT NOT NULL DEFAULT '',
          current_owner_login TEXT NOT NULL DEFAULT '',
          previous_license_sha256 TEXT NOT NULL DEFAULT '',
          previous_owner_login TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL, details TEXT NOT NULL DEFAULT ''
        );
        """)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(github_repositories)")}
        additions = {
            "legal_status": "TEXT NOT NULL DEFAULT 'pending'",
            "legal_license_spdx": "TEXT NOT NULL DEFAULT ''",
            "legal_license_path": "TEXT NOT NULL DEFAULT ''",
            "legal_license_sha256": "TEXT NOT NULL DEFAULT ''",
            "legal_commit_sha": "TEXT NOT NULL DEFAULT ''",
            "legal_captured_at": "TEXT",
            "legal_owner_login": "TEXT NOT NULL DEFAULT ''",
            "legal_owner_credit": "TEXT NOT NULL DEFAULT ''",
            "legal_alert": "TEXT NOT NULL DEFAULT ''",
            "last_legal_audit_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE github_repositories ADD COLUMN {name} {declaration}")
        # Existing records have no archived license text/commit evidence. Fail closed.
        conn.execute("""
          UPDATE github_repositories SET legal_status='restricted'
          WHERE legal_status='pending'
        """)
        conn.execute("""
          UPDATE github_files SET distribution_mode='external_link',
                 rights_status=CASE
                   WHEN (SELECT legal_license_spdx FROM github_repositories
                         WHERE id=github_files.repository_id)='' THEN 'unlicensed'
                   ELSE 'review_required' END,
                 download_status='external_only'
          WHERE repository_id IN (SELECT id FROM github_repositories
                                  WHERE legal_status!='approved')
            AND distribution_mode='hosted'
        """)


async def capture_snapshot(repository_id: int, full_name: str):
    ensure_schema()
    async with httpx.AsyncClient(timeout=45, headers=_headers(), follow_redirects=True) as client:
        meta = await _get_json(client, f"{API}/repos/{full_name}")
        branch = meta.get("default_branch") or "main"
        ref = await _get_json(client, f"{API}/repos/{full_name}/git/ref/heads/{quote(branch, safe='')}")
        commit_sha = ref.get("object", {}).get("sha", "")
        if not commit_sha:
            raise ValueError("No se pudo determinar el commit exacto del repositorio")
        license_path = None
        license_text = None
        for candidate in LICENSE_FILES:
            license_text = await _get_text(client, full_name, candidate, commit_sha)
            if license_text is not None:
                license_path = candidate
                break
        readme_path = None
        readme_text = ""
        for candidate in README_FILES:
            value = await _get_text(client, full_name, candidate, commit_sha)
            if value is not None:
                readme_path, readme_text = candidate, value
                break
        api_license = meta.get("license") or {}
        license_id = _license_id(license_text, api_license.get("spdx_id"))
        license_hash = hashlib.sha256((license_text or "").encode("utf-8")).hexdigest() if license_text is not None else ""
        readme_hash = hashlib.sha256(readme_text.encode("utf-8")).hexdigest()
        owner = meta.get("owner") or {}
        owner_login = str(owner.get("login") or "")
        owner_profile = await _get_json(client, f"{API}/users/{quote(owner_login)}") if owner_login else {}
        owner_name = str(owner_profile.get("name") or owner_login)
        decision = "approved" if license_path and license_text and license_id in OPEN_LICENSES and not _has_scope_exclusions(readme_text) else "restricted"
        if not license_path:
            reason = "No se encontró un archivo LICENSE/COPYING explícito."
        elif not license_id:
            reason = "La licencia no se pudo identificar como licencia abierta que permita distribución y adaptación."
        elif _has_scope_exclusions(readme_text):
            reason = "El README contiene una posible exclusión de contenidos; requiere revisión."
        else:
            reason = "Licencia abierta identificada; comprobar su alcance y respetar sus condiciones."
        metadata_snapshot = {
            "full_name": meta.get("full_name"), "html_url": meta.get("html_url"),
            "description": meta.get("description"), "default_branch": branch,
            "license_api": api_license, "owner": owner, "owner_profile": owner_profile,
            "repository_created_at": meta.get("created_at"),
            "repository_updated_at": meta.get("updated_at"),
        }
        stamp = now()
        with connect(DB_PATH) as conn:
            previous = conn.execute(
                "SELECT * FROM github_legal_snapshots WHERE repository_id=? ORDER BY id DESC LIMIT 1",
                (repository_id,),
            ).fetchone()
            if previous and (
                previous["license_sha256"] != license_hash
                or previous["owner_login"] != owner_login
                or previous["readme_sha256"] != readme_hash
                or previous["license_spdx"] != (license_id or "")
            ):
                decision = "review_required"
                reason = "Han cambiado LICENSE, README, la licencia declarada o el propietario desde la instantánea anterior."
                conn.execute(
                    "UPDATE github_repositories SET legal_status='blocked',legal_alert=? WHERE id=?",
                    (reason, repository_id),
                )
                conn.execute("""
                  UPDATE github_files SET distribution_mode='external_link',
                    rights_status='review_required',download_status='external_only'
                  WHERE repository_id=?
                """, (repository_id,))
            conn.execute("""
              INSERT OR IGNORE INTO github_legal_snapshots
              (repository_id,commit_sha,captured_at,license_path,license_text,license_sha256,
               license_spdx,readme_path,readme_text,readme_sha256,repository_metadata_json,
               owner_login,owner_name,author_credit,license_url,decision,decision_reason)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                repository_id, commit_sha, stamp, license_path or "", license_text or "",
                license_hash, license_id or "", readme_path, readme_text, readme_hash,
                json.dumps(metadata_snapshot, ensure_ascii=False), owner_login, owner_name,
                owner_name, (api_license.get("url") or ""), decision, reason,
            ))
            current = conn.execute(
                "SELECT legal_status FROM github_repositories WHERE id=?", (repository_id,)
            ).fetchone()
            # First snapshot is the only automatic approval. Changes require human review.
            if not previous:
                legal_status = "approved" if decision == "approved" else "restricted"
            elif decision == "review_required":
                legal_status = "blocked"
            else:
                legal_status = current["legal_status"]
            conn.execute("""
              UPDATE github_repositories SET legal_status=?,legal_license_spdx=?,
                legal_license_path=?,legal_license_sha256=?,legal_commit_sha=?,
                legal_captured_at=?,legal_owner_login=?,legal_owner_credit=?,
                last_legal_audit_at=?,legal_alert=CASE WHEN ?='approved' THEN '' ELSE legal_alert END
              WHERE id=?
            """, (
                legal_status, license_id or "", license_path or "", license_hash,
                commit_sha, stamp, owner_login, owner_name, stamp, legal_status, repository_id,
            ))
            conn.execute("""
              INSERT INTO github_legal_audits
              (repository_id,audited_at,current_commit_sha,current_license_sha256,current_owner_login,
               previous_license_sha256,previous_owner_login,status,details)
              VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                repository_id, stamp, commit_sha, license_hash, owner_login,
                previous["license_sha256"] if previous else "",
                previous["owner_login"] if previous else "", legal_status, reason,
            ))
        return {"status": legal_status, "license_spdx": license_id or "",
                "license_path": license_path or "", "commit_sha": commit_sha,
                "owner_login": owner_login, "owner_credit": owner_name, "reason": reason}


async def audit_repository(repository_id: int, full_name: str):
    return await capture_snapshot(repository_id, full_name)


def approve_current_snapshot(repository_id: int):
    ensure_schema()
    stamp = now()
    with connect(DB_PATH) as conn:
        snapshot = conn.execute(
            "SELECT * FROM github_legal_snapshots WHERE repository_id=? ORDER BY id DESC LIMIT 1",
            (repository_id,),
        ).fetchone()
        if not snapshot or snapshot["decision"] not in {"approved", "review_required"}:
            raise ValueError("No existe una instantánea legal revisable.")
        if (
            not snapshot["license_text"]
            or snapshot["license_spdx"] not in OPEN_LICENSES
            or _has_scope_exclusions(snapshot["readme_text"])
        ):
            raise ValueError("La última instantánea no contiene una licencia abierta aplicable a las partituras.")
        conn.execute(
            """UPDATE github_repositories SET legal_status='approved',legal_alert='',
               legal_license_spdx=?,legal_license_path=?,legal_license_sha256=?,
               legal_commit_sha=?,legal_captured_at=?,legal_owner_login=?,
               legal_owner_credit=? WHERE id=?""",
            (snapshot["license_spdx"], snapshot["license_path"], snapshot["license_sha256"],
             snapshot["commit_sha"], snapshot["captured_at"], snapshot["owner_login"],
             snapshot["author_credit"], repository_id),
        )
        conn.execute(
            """INSERT INTO github_legal_audits
               (repository_id,audited_at,current_commit_sha,current_license_sha256,
                current_owner_login,previous_license_sha256,previous_owner_login,status,details)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (repository_id, stamp, snapshot["commit_sha"], snapshot["license_sha256"],
             snapshot["owner_login"], snapshot["license_sha256"], snapshot["owner_login"],
             "manually_approved", "Aprobación manual de la instantánea legal más reciente."),
        )


def assert_repository_approved(repository_id: int):
    ensure_schema()
    with connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT legal_status,legal_alert FROM github_repositories WHERE id=?",
            (repository_id,),
        ).fetchone()
    if not row or row["legal_status"] != "approved":
        reason = row["legal_alert"] if row else "Repositorio inexistente"
        raise ValueError("Repositorio bloqueado: falta una licencia abierta explícita y snapshot legal aprobado. " + str(reason or ""))


def reconcile_database():
    """Fail-closed repair for old databases; preserve source links and legal evidence."""
    ensure_schema()
    removed = 0
    with connect(DB_PATH) as conn:
        rows = conn.execute("""
          SELECT f.id,f.local_path FROM github_files f
          JOIN github_repositories r ON r.id=f.repository_id
          WHERE r.legal_status!='approved' AND f.local_path IS NOT NULL
        """).fetchall()
        root = DOWNLOAD_DIR.resolve()
        for row in rows:
            path = Path(row["local_path"]).resolve()
            if path.is_relative_to(root):
                path.unlink(missing_ok=True)
                removed += 1
        conn.execute("""
          UPDATE github_files SET local_path=NULL,download_status='external_only',
            distribution_mode='external_link',
            rights_status=CASE
              WHEN (SELECT legal_license_spdx FROM github_repositories
                    WHERE id=github_files.repository_id)='' THEN 'unlicensed'
              ELSE 'review_required' END
          WHERE repository_id IN (SELECT id FROM github_repositories WHERE legal_status!='approved')
        """)
    return {"local_files_removed": removed}
