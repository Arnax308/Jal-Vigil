"""SQLite store for raw API responses.

Every external response is kept with the time it was fetched. Fresh entries skip a network call;
stale entries are served (and labelled stale) only when every live attempt has failed.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings


@dataclass(frozen=True)
class CachedEntry:
    data: Any
    source: str
    fetched_at: float


class ResponseCache:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS responses ("
                " key TEXT PRIMARY KEY, source TEXT NOT NULL,"
                " fetched_at REAL NOT NULL, body TEXT NOT NULL)"
            )
            self._conn.commit()

    def get(self, key: str) -> CachedEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body, source, fetched_at FROM responses WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return CachedEntry(json.loads(row[0]), row[1], row[2])

    def put(self, key: str, source: str, data: Any) -> float:
        fetched_at = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses (key, source, fetched_at, body) VALUES (?, ?, ?, ?)",
                (key, source, fetched_at, json.dumps(data)),
            )
            self._conn.commit()
        return fetched_at


cache = ResponseCache(settings.db_path)
