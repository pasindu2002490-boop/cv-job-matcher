from __future__ import annotations

import os
from pathlib import Path

from .sqlite import SQLiteRepository


def create_repository() -> SQLiteRepository:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        from .postgres import PostgresRepository

        return PostgresRepository(database_url)

    database_path = Path(os.getenv("TASK_DB_PATH", "web_data/state/tasks.sqlite3"))
    return SQLiteRepository(database_path)
