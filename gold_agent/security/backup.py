from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from gold_agent.config import settings
from gold_agent.infra.database import conn, db_lock


def create_backup(target_dir: Path | None = None) -> Path:
    if settings.resolved_db_path() == ":memory:":
        raise RuntimeError("内存数据库不能创建持久化备份")
    directory = target_dir or settings.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"gold_agent_{time.strftime('%Y%m%d_%H%M%S')}.db"
    with db_lock:
        backup_conn = sqlite3.connect(target)
        try:
            conn.backup(backup_conn)
            result = backup_conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"备份完整性检查失败: {result}")
        finally:
            backup_conn.close()
    return target


def monitor_backups() -> None:
    if settings.backup_interval_hours <= 0 or settings.resolved_db_path() == ":memory:":
        return
    while True:
        try:
            create_backup()
        except Exception as exc:
            print(f"[数据库备份失败] {exc}")
        time.sleep(settings.backup_interval_hours * 3600)
