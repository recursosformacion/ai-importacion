from __future__ import annotations

"""Detached, persistent worker for the GitHub repository processing queue.

The worker owns no browser request or Uvicorn task. It polls SQLite, so closing
the page or restarting the web server does not cancel an active repository job.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.db import connect
from app.github_processing import DB_PATH, ensure_processing_schema, process_repository

IDLE_SECONDS = max(15, int(os.getenv("AI_IMPORTACION_GITHUB_WORKER_IDLE_SECONDS", "90")))
POLL_SECONDS = 1.0


def _acquire_singleton_lock():
    lock_path = Path(DB_PATH).resolve().parent / "github-processing-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    for attempt in range(30):
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except (OSError, BlockingIOError):
            if attempt == 29:
                handle.close()
                return None
            time.sleep(0.5)
    handle.close()
    return None


def _next_repository_id() -> int | None:
    with connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT repository_id FROM github_processing_jobs
               WHERE status IN ('queued','pending')
               ORDER BY CASE WHEN status='queued' THEN 0 ELSE 1 END, updated_at, repository_id
               LIMIT 1"""
        ).fetchone()
    return int(row["repository_id"]) if row else None


def main() -> int:
    logging.basicConfig(
        level=os.getenv("AI_IMPORTACION_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    log = logging.getLogger("github-worker")
    lock = _acquire_singleton_lock()
    if lock is None:
        log.info("Ya hay un trabajador GitHub activo; no se inicia otro.")
        return 0

    try:
        ensure_processing_schema()
        # A running row at worker startup belongs to a worker process that died
        # before releasing its lease. Requeue it; downloaded files and analyses
        # remain in SQLite and are reused by the next execution.
        with connect(DB_PATH) as conn:
            conn.execute(
                """UPDATE github_processing_jobs
                   SET status='queued',stage='queued',current_path='',updated_at=?
                   WHERE status='running'""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )
        log.info("Trabajador GitHub persistente iniciado. Cola SQLite: %s", DB_PATH)
        idle_since = time.monotonic()
        while True:
            repository_id = _next_repository_id()
            if repository_id is None:
                if time.monotonic() - idle_since >= IDLE_SECONDS:
                    log.info("Cola GitHub vacía; trabajador detenido por inactividad.")
                    return 0
                time.sleep(POLL_SECONDS)
                continue

            idle_since = time.monotonic()
            with connect(DB_PATH) as conn:
                claimed = conn.execute(
                    """UPDATE github_processing_jobs
                       SET status='running',stage='starting',updated_at=?
                       WHERE repository_id=? AND status IN ('queued','pending')""",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), repository_id),
                ).rowcount
            if not claimed:
                continue
            log.info("Comienza repositorio id=%s", repository_id)
            process_repository(repository_id)
            with connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT status,error_text FROM github_processing_jobs WHERE repository_id=?",
                    (repository_id,),
                ).fetchone()
            if row:
                log.info("Finaliza repositorio id=%s estado=%s%s", repository_id,
                         row["status"], (" error=" + str(row["error_text"])) if row["error_text"] else "")
            idle_since = time.monotonic()
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
