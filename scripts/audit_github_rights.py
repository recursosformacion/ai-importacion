from __future__ import annotations

"""Audit every registered GitHub source and fail closed on changed legal terms.

Run from the project root:
    .venv\\Scripts\\python.exe -m scripts.audit_github_rights
"""

import asyncio
import json

from app.github_catalog import ensure_schema
from app.db import connect
from app.github_catalog import DB_PATH
from app.github_legal import audit_repository, reconcile_database


async def main():
    ensure_schema()
    with connect(DB_PATH) as conn:
        repositories = conn.execute(
            "SELECT id,full_name FROM github_repositories ORDER BY id"
        ).fetchall()
    results = []
    for repository in repositories:
        try:
            result = await audit_repository(repository["id"], repository["full_name"])
            results.append({"repository": repository["full_name"], **result})
        except Exception as exc:
            results.append({
                "repository": repository["full_name"],
                "status": "audit_error",
                "reason": str(exc),
            })
    cleanup = reconcile_database()
    print(json.dumps({"audits": results, "cleanup": cleanup}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
