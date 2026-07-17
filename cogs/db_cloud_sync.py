"""
Sync local SQLite files to Neon PostgreSQL (DATABASE_URL).

The bot keeps using SQLite at runtime. Neon stores binary copies of each
*.sqlite file so Render restarts do not wipe alliance/user/gift data.

Env:
  DATABASE_URL or NEON_DATABASE_URL  — Neon pooled connection string
  CLOUD_SYNC_INTERVAL_MINUTES        — default 5
  CLOUD_SYNC_FORCE_RESTORE           — if 1, overwrite local files from Neon
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Iterable, Optional

from discord.ext import commands, tasks

logger = logging.getLogger("bot")

BACKUP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sqlite_file_backups (
    filename TEXT PRIMARY KEY,
    data BYTEA NOT NULL,
    size_bytes BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def get_database_url() -> Optional[str]:
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    return url.strip()


def is_cloud_sync_enabled() -> bool:
    return bool(get_database_url())


def get_database_dir() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("DATABASE_DIR") or "db"))


def _unique_db_dirs() -> list[str]:
    """Return distinct DB directories (DATABASE_DIR and ./db may differ if symlink failed)."""
    seen: set[str] = set()
    dirs: list[str] = []
    for raw in (get_database_dir(), os.path.abspath("db")):
        path = os.path.realpath(raw) if os.path.exists(raw) else os.path.abspath(raw)
        if path not in seen:
            seen.add(path)
            dirs.append(path)
    return dirs


def _pg_connect():
    import psycopg

    url = get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Neon pooler + SSL; connect_timeout avoids hanging startup on cold compute.
    return psycopg.connect(url, connect_timeout=45)


def _ensure_backup_table(conn) -> None:
    conn.execute(BACKUP_TABLE_SQL)
    conn.commit()


def _list_sqlite_names(db_dir: str) -> list[str]:
    if not os.path.isdir(db_dir):
        return []
    return sorted(
        name
        for name in os.listdir(db_dir)
        if name.endswith(".sqlite") and os.path.isfile(os.path.join(db_dir, name))
    )


def checkpoint_sqlite_files(db_dirs: Optional[Iterable[str]] = None) -> None:
    """Flush WAL into the main DB file before reading bytes for upload."""
    for db_dir in db_dirs or _unique_db_dirs():
        for name in _list_sqlite_names(db_dir):
            path = os.path.join(db_dir, name)
            try:
                with sqlite3.connect(path, timeout=30) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.warning("WAL checkpoint failed for %s: %s", path, exc)


def restore_from_cloud(db_dir: Optional[str] = None) -> int:
    """
    Restore *.sqlite files from Neon into local disk.
    Skips files that already exist locally (unless CLOUD_SYNC_FORCE_RESTORE=1).
    Returns number of files written.
    Raises on connection / query failures so startup can report them.
    """
    if not is_cloud_sync_enabled():
        return 0

    force = os.environ.get("CLOUD_SYNC_FORCE_RESTORE", "").strip() in ("1", "true", "yes")
    targets = [os.path.abspath(db_dir)] if db_dir else _unique_db_dirs()
    for target in targets:
        os.makedirs(target, exist_ok=True)

    with _pg_connect() as conn:
        _ensure_backup_table(conn)
        rows = conn.execute(
            "SELECT filename, data FROM sqlite_file_backups"
        ).fetchall()

    restored = 0
    for filename, data in rows:
        safe_name = os.path.basename(str(filename))
        if not safe_name.endswith(".sqlite"):
            continue
        payload = bytes(data) if data is not None else b""
        if not payload:
            continue

        for target in targets:
            path = os.path.join(target, safe_name)
            if not force and os.path.exists(path) and os.path.getsize(path) > 0:
                continue
            with open(path, "wb") as fh:
                fh.write(payload)
            restored += 1
            logger.info(
                "Restored %s from Neon (%s bytes) -> %s",
                safe_name,
                len(payload),
                path,
            )

    return restored


def push_to_cloud(db_dirs: Optional[Iterable[str]] = None) -> int:
    """Upload local *.sqlite files to Neon. Returns number of files uploaded."""
    if not is_cloud_sync_enabled():
        return 0

    dirs = list(db_dirs) if db_dirs is not None else _unique_db_dirs()
    checkpoint_sqlite_files(dirs)

    # Prefer the newest/largest copy when the same filename appears in multiple dirs.
    files: dict[str, tuple[str, int, float]] = {}
    for db_dir in dirs:
        for name in _list_sqlite_names(db_dir):
            path = os.path.join(db_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            prev = files.get(name)
            if prev is None or st.st_mtime > prev[2] or (
                st.st_mtime == prev[2] and st.st_size > prev[1]
            ):
                files[name] = (path, st.st_size, st.st_mtime)

    if not files:
        return 0

    uploaded = 0
    try:
        with _pg_connect() as conn:
            _ensure_backup_table(conn)
            for name, (path, size, _) in files.items():
                with open(path, "rb") as fh:
                    data = fh.read()
                conn.execute(
                    """
                    INSERT INTO sqlite_file_backups (filename, data, size_bytes, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (filename) DO UPDATE SET
                        data = EXCLUDED.data,
                        size_bytes = EXCLUDED.size_bytes,
                        updated_at = NOW()
                    """,
                    (name, data, len(data)),
                )
                uploaded += 1
            conn.commit()
    except Exception as exc:
        logger.error("Neon push failed: %s", exc)
        print(f"  Neon push failed: {exc}", flush=True)
        return 0

    logger.info("Pushed %s SQLite file(s) to Neon", uploaded)
    return uploaded


class CloudDatabaseSync(commands.Cog):
    """Periodic Neon backup of SQLite files."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_push_ok: Optional[float] = None
        if is_cloud_sync_enabled():
            minutes = 5
            raw = os.environ.get("CLOUD_SYNC_INTERVAL_MINUTES", "5").strip()
            try:
                minutes = max(1, int(raw))
            except ValueError:
                minutes = 5
            self.sync_loop.change_interval(minutes=minutes)
            self.sync_loop.start()
            logger.info("Neon SQLite sync enabled (every %s min)", minutes)
        else:
            logger.info("Neon SQLite sync disabled (set DATABASE_URL to enable)")

    def cog_unload(self):
        if self.sync_loop.is_running():
            self.sync_loop.cancel()
        if is_cloud_sync_enabled():
            try:
                n = push_to_cloud()
                if n:
                    print(f"  Final Neon sync: {n} file(s)", flush=True)
            except Exception as exc:
                logger.error("Final Neon sync failed: %s", exc)

    @tasks.loop(minutes=5)
    async def sync_loop(self):
        if not is_cloud_sync_enabled():
            return
        try:
            n = await asyncio.to_thread(push_to_cloud)
            if n:
                self._last_push_ok = time.time()
                logger.info("Neon sync uploaded %s file(s)", n)
        except Exception as exc:
            logger.error("Neon sync loop error: %s", exc)

    @sync_loop.before_loop
    async def before_sync_loop(self):
        await self.bot.wait_until_ready()
        # Push once soon after ready so first boot data is not only local.
        if is_cloud_sync_enabled():
            try:
                n = await asyncio.to_thread(push_to_cloud)
                if n:
                    self._last_push_ok = time.time()
                    print(f"  Neon sync: uploaded {n} SQLite file(s)", flush=True)
            except Exception as exc:
                logger.error("Initial Neon sync failed: %s", exc)


async def setup(bot: commands.Bot):
    await bot.add_cog(CloudDatabaseSync(bot))
