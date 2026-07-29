from __future__ import annotations

import threading
from contextlib import contextmanager

from .sqlite import SQLiteRepository
from .sqlite_schema import SCHEMA_SQL


POSTGRES_SCHEMA_SQL = "\n".join(
    line for line in SCHEMA_SQL.splitlines() if not line.startswith("PRAGMA ")
)


class _PsycopgConnectionAdapter:
    def __init__(self, connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params=()):
        translated = sql.replace("?", "%s")
        return self._connection.execute(translated, params)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            sql = statement.strip()
            if sql:
                self.execute(sql)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class PostgresRepository(SQLiteRepository):
    """Repository adapter for PostgreSQL/Supabase."""

    def __init__(self, database_url: str, *, initialize: bool = True):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional cloud dependency
            raise RuntimeError(
                "Install psycopg[binary] to use the PostgreSQL repository"
            ) from exc

        self.database = database_url
        self._lock = threading.RLock()
        self._connection = _PsycopgConnectionAdapter(
            psycopg.connect(database_url, row_factory=dict_row)
        )
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(POSTGRES_SCHEMA_SQL)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
