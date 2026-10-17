"""Atomic list storage and a durable, reasoned history of user outcomes.

Importing this module neither reads credentials nor creates or changes files.
Existing exclusion entries are kept as-is; their old reasons are unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Iterable

from .paths import (
    BASE_DIR, GONDERILER_PATH, IDSIFRE_PATH, KARALIST_PATH,
    STATUS_DB_PATH, TAKIPLIST_PATH,
)


_STORAGE_LOCK = threading.RLock()


def atomic_write_text(path, content: str, encoding: str = "utf-8") -> None:
    """Replace a file only after its complete replacement has been flushed.

The temporary file lives beside the destination, so replacement is atomic on
the same filesystem. A failed write leaves the original destination intact.
"""
    path = Path(path)
    with _STORAGE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding=encoding, newline="", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _normalize_usernames(usernames: Iterable[str]) -> list[str]:
    if isinstance(usernames, str):
        usernames = usernames.splitlines()
    normalized = []
    seen = set()
    for entry in usernames:
        # Accept pasted multiline entries without ever joining two usernames.
        for line in entry.splitlines():
            username = line.strip().lstrip("\ufeff").strip()
            key = username.casefold()
            if username and key not in seen:
                normalized.append(username)
                seen.add(key)
    return normalized


def read_takiplist(path=None) -> list[str]:
    path = Path(path) if path is not None else Path(TAKIPLIST_PATH)
    with _STORAGE_LOCK:
        if not path.exists():
            return []
        return _normalize_usernames(path.read_text(encoding="utf-8").splitlines())


def save_takiplist(usernames: Iterable[str], path=None) -> list[str]:
    path = Path(path) if path is not None else Path(TAKIPLIST_PATH)
    normalized = _normalize_usernames(usernames)
    # The final newline is intentional: legacy appenders cannot fuse the last
    # existing username with a newly collected username.
    content = "".join(f"{username}\n" for username in normalized)
    with _STORAGE_LOCK:
        atomic_write_text(path, content)
    return normalized


def append_to_takiplist(users: Iterable[str]) -> list[str]:
    """Merge and replace under one lock, including legacy files without '\\n'."""
    with _STORAGE_LOCK:
        return save_takiplist(read_takiplist() + _normalize_usernames(users))


def remove_from_takiplist(username: str) -> None:
    key = username.strip().casefold()
    with _STORAGE_LOCK:
        if Path(TAKIPLIST_PATH).exists():
            save_takiplist(user for user in read_takiplist() if user.casefold() != key)


def write_to_karalist(username: str) -> None:
    with _STORAGE_LOCK:
        excluded = read_takiplist(KARALIST_PATH)
        save_takiplist(excluded + [username], KARALIST_PATH)


def read_idsifre(path=None):
    """Password-file login is unsupported in the public application."""
    raise RuntimeError("Şifre dosyası kullanılmaz; tarayıcıdan elle giriş yapın.")


def read_all_post_links() -> list[str]:
    path = Path(GONDERILER_PATH)
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def temizle_takiplist_karalisteye_gore() -> None:
    with _STORAGE_LOCK:
        if not Path(TAKIPLIST_PATH).exists():
            return
        users = read_takiplist()
        excluded = {user.casefold() for user in read_takiplist(KARALIST_PATH)}
        remaining = save_takiplist(user for user in users if user.casefold() not in excluded)
    print(f"Takiplist.txt temizlendi. {len(users) - len(remaining)} kişi çıkarıldı.")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_status (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS user_status_events_username
    ON user_status_events(username, id);
"""


def record_user_status(
    username: str, status: str, reason: str, detail: str = "",
    increment_attempt: bool = False,
) -> dict:
    """Persist the latest outcome and its history in one SQLite transaction.

Only real attempts should set ``increment_attempt``. Updating the outcome of
an already-counted attempt leaves its count unchanged. No legacy list entry
is inferred to have any particular status or reason.
"""
    username = username.strip()
    if not username or not status.strip() or not reason.strip():
        raise ValueError("Kullanıcı adı, durum ve neden boş olamaz")
    with _STORAGE_LOCK:
        updated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        database_path = Path(STATUS_DB_PATH)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT attempts FROM user_status WHERE username = ?", (username,),
            ).fetchone()
            attempts = (previous["attempts"] if previous else 0) + int(bool(increment_attempt))
            values = (username, status, reason, detail, attempts, updated_at)
            connection.execute(
                """INSERT INTO user_status(username, status, reason, detail, attempts, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                       status = excluded.status,
                       reason = excluded.reason,
                       detail = excluded.detail,
                       attempts = excluded.attempts,
                       updated_at = excluded.updated_at""",
                values,
            )
            connection.execute(
                """INSERT INTO user_status_events(username, status, reason, detail, attempts, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                values,
            )
            row = connection.execute(
                "SELECT * FROM user_status WHERE username = ?", (username,),
            ).fetchone()
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def get_user_statuses(limit: int = 300, search: str = "") -> list[dict]:
    """Return recent user outcomes; an untouched installation returns no rows."""
    limit = max(0, min(int(limit), 10000))
    database_path = Path(STATUS_DB_PATH)
    with _STORAGE_LOCK:
        if not limit or not database_path.exists():
            return []
        connection = sqlite3.connect(database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            # Searching is literal; '_' is common in Instagram usernames.
            needle = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = connection.execute(
                """SELECT username, status, reason, detail, attempts, updated_at
                   FROM user_status
                   WHERE username LIKE ? ESCAPE '\\'
                   ORDER BY updated_at DESC, username ASC LIMIT ?""",
                (f"%{needle}%", limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
