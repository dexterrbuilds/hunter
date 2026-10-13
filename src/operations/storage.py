"""Separate operational SQLite storage; never opens trading records."""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock

from operations.config import require

_APPLICATION_ID = 1213416516


def prepare_directory(path: Path) -> None:
    """Create a private directory or reject unsafe existing permissions."""
    require(
        not any(item.is_symlink() for item in (path, *path.parents)),
        "storage directory (symlink)",
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(
        path.is_dir() and not path.stat().st_mode & 0o077,
        "storage directory permissions (require 0700)",
    )
    require(path.stat().st_uid == os.getuid(), "storage directory owner")


class ShadowStore:
    """Single-worker WAL repository with explicit version and session recovery."""

    def __init__(self, path: Path) -> None:
        prepare_directory(path.parent)
        require(not path.is_symlink(), "shadow database (symlink)")
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        require(
            stat.S_ISREG(path.stat().st_mode) and not path.stat().st_mode & 0o077,
            "shadow database permissions (require 0600)",
        )
        self.connection = sqlite3.connect(path, timeout=2, check_same_thread=False)
        self.lock = RLock()
        self.lease: int | None = None
        try:
            self.lease = os.open(
                str(path) + ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            identifier = self.connection.execute("PRAGMA application_id").fetchone()[0]
            tables = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            require(
                identifier == _APPLICATION_ID or (identifier == 0 and not tables),
                "shadow database identity (not a trading database)",
            )
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            require(version in {0, 1}, "shadow database schema version")
            if version == 1:
                require(
                    {"sessions", "observations"}.issubset({row[0] for row in tables}),
                    "shadow database schema (missing tables)",
                )
            require(
                self.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                "shadow database integrity",
            )
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.connection:
                self.connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started TEXT NOT NULL, stopped TEXT, status TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS observations (session_id TEXT NOT NULL, mint TEXT NOT NULL, source TEXT NOT NULL, observed TEXT NOT NULL, monotonic_ns INTEGER NOT NULL, event_slot INTEGER, PRIMARY KEY(session_id,mint))"
                )
                self.connection.execute("PRAGMA user_version=1")
        except BaseException:
            self.connection.close()
            if self.lease is not None:
                os.close(self.lease)
            raise

    def start(self, session_id: str, timestamp: str) -> int:
        """Mark interrupted observation sessions; never replay their events."""
        with self.lock, self.connection:
            count = self.connection.execute(
                "UPDATE sessions SET status='interrupted' WHERE status='running'"
            ).rowcount
            self.connection.execute(
                "INSERT INTO sessions VALUES (?,?,NULL,'running')",
                (session_id, timestamp),
            )
        return count

    def record(self, rows: list[tuple]) -> None:
        """Persist a bounded batch with per-session mint deduplication."""
        with self.lock, self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)", rows
            )

    def finish(self, session_id: str, timestamp: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE sessions SET stopped=?, status='stopped' WHERE id=?",
                (timestamp, session_id),
            )

    def close(self) -> None:
        with self.lock:
            self.connection.close()
            if self.lease is not None:
                os.close(self.lease)
                self.lease = None


def storage_probe(directory: Path, minimum_free_bytes: int) -> dict[str, object]:
    """Test a disposable database, never the configured production database."""
    require(directory.exists(), "storage directory (provision it before preflight)")
    prepare_directory(directory)
    free = shutil.disk_usage(directory).free
    require(free >= minimum_free_bytes, "storage free disk space")
    with TemporaryDirectory(prefix="hunter-preflight-", dir=directory) as temporary:
        store = ShadowStore(Path(temporary) / "probe.sqlite3")
        try:
            store.start("probe", "diagnostic")
            store.record([("probe", "fake", "offline", "diagnostic", 1, None)])
            store.finish("probe", "diagnostic")
        finally:
            store.close()
        reopened = ShadowStore(Path(temporary) / "probe.sqlite3")
        reopened.close()
    return {
        "disposable_sqlite_write_restart": "passed",
        "free_bytes": free,
        "future_writes_guaranteed": False,
    }
