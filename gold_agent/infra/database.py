from __future__ import annotations

import sqlite3
import threading

from gold_agent.config import settings

db_lock = threading.RLock()
db_path = settings.resolved_db_path()
if db_path != ":memory:":
    db_path.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=" + ("WAL" if settings.sqlite_wal else "DELETE"))
conn.execute("PRAGMA synchronous=FULL")
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=5000")
c = conn.cursor()
