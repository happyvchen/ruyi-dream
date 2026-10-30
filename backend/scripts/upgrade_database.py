"""Offline database upgrade: verified backup, migrations, compaction and VACUUM.

Stop backend writers first. Usage: python scripts/upgrade_database.py /path/app.sqlite3
Does not import the application, start workers, access media or call providers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def check(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLite integrity_check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("SQLite foreign_key_check failed")


def upgrade(path: Path) -> dict:
    path = path.resolve(strict=True)
    backup = path.with_name(path.name + ".pre-upgrade-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    before = path.stat().st_size
    with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True) as source:
        check(source)
        # SQLite backup includes committed WAL content, unlike copying the main file.
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
            check(destination)
    from app.config import get_settings
    from app.db import init_db
    settings = get_settings()
    settings.database_path = path
    settings.data_dir = path.parent
    init_db()
    with sqlite3.connect(path) as db:
        check(db)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        check(db)
        reports = db.execute("SELECT payload_json FROM event_logs WHERE event_type = 'call_facts_compacted' ORDER BY id DESC LIMIT 1").fetchone()
    return {"database": str(path), "backup": str(backup), "bytesBefore": before,
            "bytesAfter": path.stat().st_size, "compaction": json.loads(reports[0]) if reports else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(upgrade(args.database), ensure_ascii=False, indent=2))
