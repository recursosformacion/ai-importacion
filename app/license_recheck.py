"""Revisión periódica de la licencia declarada por los repositorios de GitHub.

Resumen: consulta la API de GitHub, compara la licencia con la almacenada, deja
historial y reclasifica el repositorio. Si la licencia deja de permitir el
alojamiento, los recursos exportados pasan a `link_only` en el siguiente paquete.

Uso:
    python -m app.license_recheck --limit 20
    python -m app.license_recheck --days 7 --force --dry-run

Programación recomendada: una vez al día (cron), con `--limit 50`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.db import DB_PATH, connect, now
from app.license_policy import evaluate, normalize_spdx

API = "https://api.github.com"
DEFAULT_DAYS = int(os.getenv("AI_IMPORTACION_LICENSE_RECHECK_DAYS", "7"))
USER_AGENT = "OSAP-AI-Importacion/license-recheck"


def _connection(db_path: str):
    return connect(db_path)


def ensure_schema(db_path: str = DB_PATH) -> None:
    with _connection(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "github_repositories" not in tables:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS github_license_checks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              repository_id INTEGER NOT NULL REFERENCES github_repositories(id) ON DELETE CASCADE,
              checked_at TEXT NOT NULL,
              spdx_before TEXT NOT NULL DEFAULT '',
              spdx_after TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              changed INTEGER NOT NULL DEFAULT 0,
              http_status INTEGER,
              note TEXT
            )
            """
        )
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(github_repositories)")
        }
        added_status = False
        for name, ddl in (
            ("license_checked_at", "TEXT"),
            ("license_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("license_previous_spdx", "TEXT NOT NULL DEFAULT ''"),
            ("license_changed_at", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE github_repositories ADD COLUMN {name} {ddl}")
                added_status = added_status or name == "license_status"
        if added_status:
            for row in conn.execute("SELECT id, license_spdx FROM github_repositories"):
                conn.execute(
                    "UPDATE github_repositories SET license_status=? WHERE id=?",
                    (evaluate(row["license_spdx"]), row["id"]),
                )


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_repo_license(
    client: httpx.AsyncClient, full_name: str
) -> tuple[int, str, str]:
    """Devuelve `(http_status, spdx, license_name)` del repositorio."""
    response = await client.get(f"{API}/repos/{full_name}")
    if response.status_code != 200:
        return response.status_code, "", ""
    payload = response.json()
    license_data = payload.get("license") or {}
    return (
        200,
        str(license_data.get("spdx_id") or ""),
        str(license_data.get("name") or ""),
    )


def _is_due(checked_at: str | None, days: int) -> bool:
    if not checked_at:
        return True
    try:
        moment = datetime.fromisoformat(checked_at)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= datetime.now(UTC) - timedelta(days=days)


def _record_check(
    conn: Any,
    *,
    repository_id: int,
    before: str,
    after: str,
    status: str,
    changed: bool,
    http_status: int | None,
    note: str | None,
    checked_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO github_license_checks
          (repository_id, checked_at, spdx_before, spdx_after, status, changed,
           http_status, note)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            repository_id,
            checked_at,
            before,
            after,
            status,
            int(changed),
            http_status,
            note,
        ),
    )


def _apply(
    conn: Any,
    row: Any,
    *,
    after_spdx: str,
    http_status: int,
    note: str | None,
    checked_at: str,
    dry_run: bool,
) -> dict[str, Any]:
    before = normalize_spdx(row["license_spdx"])
    after = normalize_spdx(after_spdx)
    status = evaluate(after)
    changed = before != after
    if not dry_run:
        conn.execute(
            """
            UPDATE github_repositories
            SET license_spdx=?,
                license_status=?,
                license_checked_at=?,
                license_previous_spdx=CASE WHEN ? THEN ? ELSE license_previous_spdx END,
                license_changed_at=CASE WHEN ? THEN ? ELSE license_changed_at END
            WHERE id=?
            """,
            (
                after,
                status,
                checked_at,
                int(changed),
                before,
                int(changed),
                checked_at,
                row["id"],
            ),
        )
        _record_check(
            conn,
            repository_id=int(row["id"]),
            before=before,
            after=after,
            status=status,
            changed=changed,
            http_status=http_status,
            note=note,
            checked_at=checked_at,
        )
    return {
        "repository": row["full_name"],
        "repository_id": int(row["id"]),
        "spdx_before": before,
        "spdx_after": after,
        "status": status,
        "changed": changed,
        "http_status": http_status,
        "note": note,
    }


async def recheck_due(
    *,
    db_path: str = DB_PATH,
    days: int = DEFAULT_DAYS,
    limit: int = 50,
    force: bool = False,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Revisa las licencias vencidas y devuelve el resumen de cada repositorio."""
    ensure_schema(db_path)
    with _connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, full_name, license_spdx, license_checked_at
            FROM github_repositories
            ORDER BY COALESCE(license_checked_at, '') ASC, id ASC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()

        pending = [row for row in rows if force or _is_due(row["license_checked_at"], days)]
        if not pending:
            return []

        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=30,
            headers=_headers(),
            follow_redirects=False,
            transport=transport,
        ) as client:
            for row in pending:
                http_status, spdx, license_name = await fetch_repo_license(
                    client, row["full_name"]
                )
                checked_at = now()
                if http_status == 200:
                    note = license_name or None
                elif http_status == 404:
                    note = "repositorio no encontrado o no público; se enlaza al origen"
                elif http_status == 403:
                    note = "límite de consultas de GitHub alcanzado; se reintentará"
                else:
                    note = f"GitHub devolvió HTTP {http_status}"
                if http_status == 403:
                    results.append(
                        {
                            "repository": row["full_name"],
                            "repository_id": int(row["id"]),
                            "spdx_before": normalize_spdx(row["license_spdx"]),
                            "spdx_after": normalize_spdx(row["license_spdx"]),
                            "status": evaluate(row["license_spdx"]),
                            "changed": False,
                            "http_status": http_status,
                            "note": note,
                        }
                    )
                    break
                result = _apply(
                    conn,
                    row,
                    after_spdx=spdx,
                    http_status=http_status,
                    note=note,
                    checked_at=checked_at,
                    dry_run=dry_run,
                )
                results.append(result)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Revisa la licencia declarada por los repositorios GitHub y reclasifica "
            "su alojamiento (hostable/link_only/unknown)."
        )
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--force", action="store_true", help="Revisa aunque no esté vencida.")
    parser.add_argument("--dry-run", action="store_true", help="No escribe cambios.")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    results = asyncio.run(
        recheck_due(
            db_path=args.db,
            days=max(1, args.days),
            limit=max(1, args.limit),
            force=args.force,
            dry_run=args.dry_run,
        )
    )
    if not results:
        print("Sin repositorios pendientes de revisión.")
        return 0
    for item in results:
        change = "cambiada" if item["changed"] else "sin cambios"
        print(
            f"{item['repository']}: {item['status']} ({change}, "
            f"HTTP {item['http_status']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
