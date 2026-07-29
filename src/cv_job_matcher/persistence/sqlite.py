from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .repository import (
    FINAL_REVIEW_DECISIONS,
    CreateTaskResult,
    CvObjectInput,
    CvObjectRecord,
    IdempotencyConflict,
    InvalidTransition,
    InventoryJobRecord,
    JobReviewInput,
    JobReviewRecord,
    JobUpsert,
    LeaseConflict,
    RecordNotFound,
    ResultFileInput,
    ResultFileRecord,
    ReviewCheckpointResult,
    SourceRunRecord,
    TaskClaim,
    TaskRecord,
    ensure_utc,
    utc_now,
    validate_lease_seconds,
    validate_review_decision,
)
from .sqlite_schema import SCHEMA_SQL


def _id() -> str:
    return str(uuid.uuid4())


def _dump(value: Mapping[str, Any] | Sequence[Any] | None) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def _load_object(value: str | None) -> dict[str, Any]:
    loaded = json.loads(value or "{}")
    return loaded if isinstance(loaded, dict) else {}


def _load_tuple(value: str | None) -> tuple[str, ...]:
    loaded = json.loads(value or "[]")
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded)


def _stamp(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="microseconds")


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteRepository:
    """Thread-safe local adapter implementing the production repository contract.

    This adapter is intended for development and tests. Cloud workers should use
    :class:`PostgresRepository`, because separate Cloud Run instances cannot share
    an SQLite database file.
    """

    def __init__(self, database: str | Path = ":memory:", *, initialize: bool = True):
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database,
            isolation_level=None,
            check_same_thread=False,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def create_task(
        self,
        *,
        owner_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        country: str,
        target_position: str,
        request_payload: Mapping[str, Any] | None = None,
        cv_object: CvObjectInput | None = None,
        max_attempts: int = 3,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> CreateTaskResult:
        if not owner_id or not idempotency_key or not request_fingerprint:
            raise ValueError("owner_id, idempotency_key and request_fingerprint are required")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict(
                        "task idempotency key was already used for a different request"
                    )
                return CreateTaskResult(self._task(existing), False)

            cv_object_id: str | None = None
            if cv_object is not None:
                if cv_object.owner_id != owner_id:
                    raise ValueError("CV owner_id must match task owner_id")
                if cv_object.byte_size < 0:
                    raise ValueError("CV byte_size cannot be negative")
                prior_cv = connection.execute(
                    "SELECT * FROM cv_objects WHERE bucket = ? AND object_key = ?",
                    (cv_object.bucket, cv_object.object_key),
                ).fetchone()
                if prior_cv is not None:
                    if (
                        prior_cv["owner_id"] != owner_id
                        or prior_cv["sha256"] != cv_object.sha256
                    ):
                        raise IdempotencyConflict(
                            "CV object key already refers to different content or owner"
                        )
                    cv_object_id = prior_cv["id"]
                else:
                    cv_object_id = _id()
                    connection.execute(
                        """
                        INSERT INTO cv_objects (
                            id, owner_id, bucket, object_key, original_filename,
                            content_type, byte_size, sha256, storage_provider,
                            created_at, delete_after
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cv_object_id,
                            owner_id,
                            cv_object.bucket,
                            cv_object.object_key,
                            cv_object.original_filename,
                            cv_object.content_type,
                            cv_object.byte_size,
                            cv_object.sha256,
                            cv_object.storage_provider,
                            _stamp(current),
                            _stamp(cv_object.delete_after),
                        ),
                    )

            resolved_task_id = task_id or _id()
            connection.execute(
                """
                INSERT INTO tasks (
                    id, owner_id, idempotency_key, request_fingerprint, status,
                    country, target_position, cv_object_id, request_payload,
                    progress, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, '{}', ?, ?, ?, ?)
                """,
                (
                    resolved_task_id,
                    owner_id,
                    idempotency_key,
                    request_fingerprint,
                    country,
                    target_position,
                    cv_object_id,
                    _dump(request_payload),
                    max_attempts,
                    _stamp(current),
                    _stamp(current),
                    _stamp(current),
                ),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (resolved_task_id,)
            ).fetchone()
            return CreateTaskResult(self._task(row), True)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._task(row) if row is not None else None

    def get_cv_object(self, cv_object_id: str) -> CvObjectRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM cv_objects WHERE id = ?", (cv_object_id,)
            ).fetchone()
        return self._cv_object(row) if row is not None else None

    def update_task(
        self,
        task_id: str,
        *,
        phase: str | None = None,
        progress: Mapping[str, Any] | None = None,
        cancel_requested: bool | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"task not found: {task_id}")
            next_progress = _dump(progress) if progress is not None else row["progress"]
            next_cancel = (
                int(cancel_requested)
                if cancel_requested is not None
                else row["cancel_requested"]
            )
            next_status = row["status"]
            next_phase = phase if phase is not None else row["phase"]
            if not next_phase or len(next_phase) > 80:
                raise ValueError("phase must contain between 1 and 80 characters")
            finished_at = row["finished_at"]
            if next_cancel and row["status"] == "queued":
                next_status = "cancelled"
                next_phase = "cancelled"
                finished_at = _stamp(current)
            connection.execute(
                """
                UPDATE tasks
                SET progress = ?, cancel_requested = ?, status = ?, phase = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_progress,
                    next_cancel,
                    next_status,
                    next_phase,
                    finished_at,
                    _stamp(current),
                    task_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return self._task(updated)

    def claim_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> TaskClaim | None:
        return self._claim(
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )

    def claim_next_task(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> TaskClaim | None:
        return self._claim(
            task_id=None,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )

    def _claim(
        self,
        *,
        task_id: str | None,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None,
    ) -> TaskClaim | None:
        if not worker_id:
            raise ValueError("worker_id is required")
        validate_lease_seconds(lease_seconds)
        current = ensure_utc(now or utc_now())
        current_stamp = _stamp(current)
        expiry = _stamp(current + timedelta(seconds=lease_seconds))
        lease_token = _id()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'failed', error_code = 'lease_exhausted',
                    error_message = 'Worker lease expired after maximum attempts',
                    lease_owner = '', lease_token = '', lease_expires_at = NULL,
                    finished_at = ?, updated_at = ?
                WHERE status = 'running'
                  AND lease_expires_at <= ?
                  AND attempt_count >= max_attempts
                """,
                (current_stamp, current_stamp, current_stamp),
            )
            where_id = "AND id = ?" if task_id is not None else ""
            params: list[Any] = [current_stamp, current_stamp]
            if task_id is not None:
                params.append(task_id)
            row = connection.execute(
                f"""
                SELECT * FROM tasks
                WHERE cancel_requested = 0
                  AND attempt_count < max_attempts
                  AND (
                    (status = 'queued' AND available_at <= ?)
                    OR
                    (status = 'running' AND lease_expires_at <= ?)
                  )
                  {where_id}
                ORDER BY available_at, created_at
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE tasks
                SET status = 'running', attempt_count = attempt_count + 1,
                    phase = 'starting',
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    started_at = COALESCE(started_at, ?), finished_at = NULL,
                    error_code = '', error_message = '', updated_at = ?
                WHERE id = ?
                """,
                (
                    worker_id,
                    lease_token,
                    expiry,
                    current_stamp,
                    current_stamp,
                    row["id"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (row["id"],)
            ).fetchone()
            record = self._task(claimed)
            return TaskClaim(record, lease_token)

    def renew_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> bool:
        validate_lease_seconds(lease_seconds)
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                  AND lease_owner = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    _stamp(current + timedelta(seconds=lease_seconds)),
                    _stamp(current),
                    task_id,
                    worker_id,
                    lease_token,
                    _stamp(current),
                ),
            )
            return cursor.rowcount == 1

    def complete_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        progress: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        return self._finish_task(
            task_id,
            worker_id=worker_id,
            lease_token=lease_token,
            status="succeeded",
            progress=progress,
            now=now,
        )

    def _finish_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        status: str,
        progress: Mapping[str, Any] | None,
        error_code: str = "",
        error_message: str = "",
        now: datetime | None = None,
    ) -> TaskRecord:
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self._require_task_lease(row, task_id, worker_id, lease_token, current)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, progress = ?, error_code = ?, error_message = ?,
                    phase = ?,
                    lease_owner = '', lease_token = '', lease_expires_at = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    _dump(progress) if progress is not None else row["progress"],
                    error_code,
                    error_message,
                    "complete" if status == "succeeded" else status,
                    _stamp(current),
                    _stamp(current),
                    task_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return self._task(updated)

    def fail_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: int = 30,
        now: datetime | None = None,
    ) -> TaskRecord:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self._require_task_lease(row, task_id, worker_id, lease_token, current)
            cancelled = bool(row["cancel_requested"])
            can_retry = retryable and not cancelled and row["attempt_count"] < row["max_attempts"]
            if can_retry:
                status = "queued"
                available_at = _stamp(
                    current + timedelta(seconds=retry_delay_seconds)
                )
                finished_at = None
            else:
                status = "cancelled" if cancelled else "failed"
                available_at = row["available_at"]
                finished_at = _stamp(current)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, available_at = ?, error_code = ?,
                    error_message = ?, phase = ?, lease_owner = '', lease_token = '',
                    lease_expires_at = NULL, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    available_at,
                    error_code,
                    error_message,
                    "retry_wait" if status == "queued" else status,
                    finished_at,
                    _stamp(current),
                    task_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return self._task(updated)

    @staticmethod
    def _require_task_lease(
        row: sqlite3.Row | None,
        task_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        if row is None:
            raise RecordNotFound(f"task not found: {task_id}")
        if (
            row["status"] != "running"
            or row["lease_owner"] != worker_id
            or row["lease_token"] != lease_token
            or not row["lease_expires_at"]
            or row["lease_expires_at"] <= _stamp(now)
        ):
            raise LeaseConflict("task lease is missing, expired, or owned by another worker")

    def begin_source_run(
        self,
        *,
        source: str,
        idempotency_key: str,
        worker_id: str,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> SourceRunRecord:
        if not source or not idempotency_key or not worker_id:
            raise ValueError("source, idempotency_key and worker_id are required")
        validate_lease_seconds(lease_seconds)
        current = ensure_utc(now or utc_now())
        token = _id()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_runs WHERE source = ? AND idempotency_key = ?",
                (source, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["status"] != "running":
                    return self._source_run(row)
                if row["lease_expires_at"] and row["lease_expires_at"] > _stamp(current):
                    if row["lease_owner"] == worker_id:
                        return self._source_run(row)
                    raise LeaseConflict("source run is already leased by another worker")
                connection.execute(
                    """
                    UPDATE source_runs
                    SET lease_owner = ?, lease_token = ?, lease_expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        worker_id,
                        token,
                        _stamp(current + timedelta(seconds=lease_seconds)),
                        row["id"],
                    ),
                )
                reclaimed = connection.execute(
                    "SELECT * FROM source_runs WHERE id = ?", (row["id"],)
                ).fetchone()
                return self._source_run(reclaimed)
            run_id = _id()
            connection.execute(
                """
                INSERT INTO source_runs (
                    id, source, idempotency_key, status, lease_owner,
                    lease_token, lease_expires_at, started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    idempotency_key,
                    worker_id,
                    token,
                    _stamp(current + timedelta(seconds=lease_seconds)),
                    _stamp(current),
                ),
            )
            created = connection.execute(
                "SELECT * FROM source_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return self._source_run(created)

    def finish_source_run(
        self,
        source_run_id: str,
        *,
        worker_id: str,
        lease_token: str,
        status: str,
        discovered_count: int,
        upserted_count: int,
        metrics: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        now: datetime | None = None,
    ) -> SourceRunRecord:
        if status not in {"succeeded", "failed", "partial"}:
            raise ValueError("source run terminal status is invalid")
        if discovered_count < 0 or upserted_count < 0:
            raise ValueError("source run counts cannot be negative")
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_runs WHERE id = ?", (source_run_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"source run not found: {source_run_id}")
            if (
                row["status"] != "running"
                or row["lease_owner"] != worker_id
                or row["lease_token"] != lease_token
                or not row["lease_expires_at"]
                or row["lease_expires_at"] <= _stamp(current)
            ):
                raise LeaseConflict("source run lease is missing, expired, or invalid")
            connection.execute(
                """
                UPDATE source_runs
                SET status = ?, finished_at = ?, lease_expires_at = NULL,
                    discovered_count = ?, upserted_count = ?, metrics = ?,
                    error_code = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    _stamp(current),
                    discovered_count,
                    upserted_count,
                    _dump(metrics),
                    error_code,
                    error_message,
                    source_run_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM source_runs WHERE id = ?", (source_run_id,)
            ).fetchone()
            return self._source_run(updated)

    def upsert_jobs(
        self,
        source_run_id: str,
        jobs: Iterable[JobUpsert],
        *,
        seen_at: datetime | None = None,
        ttl: timedelta = timedelta(days=2),
    ) -> list[InventoryJobRecord]:
        if ttl.total_seconds() <= 0:
            raise ValueError("job inventory TTL must be positive")
        current = ensure_utc(seen_at or utc_now())
        rows: list[InventoryJobRecord] = []
        with self._transaction() as connection:
            source_run = connection.execute(
                "SELECT * FROM source_runs WHERE id = ?", (source_run_id,)
            ).fetchone()
            if source_run is None:
                raise RecordNotFound(f"source run not found: {source_run_id}")
            for item in jobs:
                if item.source != source_run["source"]:
                    raise ValueError(
                        f"job source {item.source!r} does not match source run "
                        f"{source_run['source']!r}"
                    )
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
                    (item.source, item.source_job_id),
                ).fetchone()
                if existing is None:
                    job_id = _id()
                    connection.execute(
                        """
                        INSERT INTO jobs (
                            id, source, source_job_id, title, company, location,
                            country, url, description, salary, job_type,
                            published_at, detail_page_verified, content_hash,
                            raw_payload, first_seen_at, last_seen_at, expires_at,
                            is_active, last_source_run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, 1, ?)
                        """,
                        (
                            job_id,
                            item.source,
                            item.source_job_id,
                            item.title,
                            item.company,
                            item.location,
                            item.country,
                            item.url,
                            item.description,
                            item.salary,
                            item.job_type,
                            _stamp(item.published_at) if item.published_at else None,
                            int(item.detail_page_verified),
                            item.content_hash,
                            _dump(item.raw_payload),
                            _stamp(current),
                            _stamp(current),
                            _stamp(current + ttl),
                            source_run_id,
                        ),
                    )
                else:
                    job_id = existing["id"]
                    connection.execute(
                        """
                        UPDATE jobs
                        SET title = ?, company = ?, location = ?, country = ?,
                            url = ?, description = ?, salary = ?, job_type = ?,
                            published_at = ?, detail_page_verified = ?,
                            content_hash = ?, raw_payload = ?, last_seen_at = ?,
                            expires_at = ?, is_active = 1,
                            last_source_run_id = ?
                        WHERE id = ?
                        """,
                        (
                            item.title,
                            item.company,
                            item.location,
                            item.country,
                            item.url,
                            item.description,
                            item.salary,
                            item.job_type,
                            _stamp(item.published_at) if item.published_at else None,
                            int(item.detail_page_verified),
                            item.content_hash,
                            _dump(item.raw_payload),
                            _stamp(current),
                            _stamp(current + ttl),
                            source_run_id,
                            job_id,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                rows.append(self._job(row))
        return rows

    def list_active_jobs(
        self,
        *,
        country: str,
        seen_since: datetime | None = None,
        limit: int = 10_000,
        now: datetime | None = None,
    ) -> list[InventoryJobRecord]:
        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be between 1 and 100000")
        current = ensure_utc(now or utc_now())
        params: list[Any] = [country.casefold(), _stamp(current)]
        seen_clause = ""
        if seen_since is not None:
            seen_clause = "AND last_seen_at >= ?"
            params.append(_stamp(seen_since))
        params.append(limit)
        with self._lock:
            found = self._connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE lower(country) = ?
                  AND is_active = 1
                  AND expires_at > ?
                  {seen_clause}
                ORDER BY last_seen_at DESC, source, source_job_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._job(row) for row in found]

    def snapshot_active_jobs(
        self,
        task_id: str,
        *,
        country: str,
        seen_since: datetime | None = None,
        limit: int = 10_000,
        now: datetime | None = None,
    ) -> list[InventoryJobRecord]:
        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be between 1 and 100000")
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise RecordNotFound(f"task not found: {task_id}")
            if task["job_snapshot_at"] is not None:
                rows = connection.execute(
                    """
                    SELECT job_snapshot FROM task_jobs
                    WHERE task_id = ?
                    ORDER BY snapshot_order
                    """,
                    (task_id,),
                ).fetchall()
                return [self._job_from_snapshot(row["job_snapshot"]) for row in rows]

            params: list[Any] = [country.casefold(), _stamp(current)]
            seen_clause = ""
            if seen_since is not None:
                seen_clause = "AND last_seen_at >= ?"
                params.append(_stamp(seen_since))
            params.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE lower(country) = ?
                  AND is_active = 1
                  AND expires_at > ?
                  {seen_clause}
                ORDER BY last_seen_at DESC, source, source_job_id
                LIMIT ?
                """,
                params,
            ).fetchall()
            records = [self._job(row) for row in rows]
            for index, record in enumerate(records):
                connection.execute(
                    """
                    INSERT INTO task_jobs (
                        task_id, job_id, snapshot_order, content_hash,
                        job_snapshot, snapshotted_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        record.id,
                        index,
                        record.content_hash,
                        self._dump_job_snapshot(record),
                        _stamp(current),
                    ),
                )
            connection.execute(
                """
                UPDATE tasks
                SET job_snapshot_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (_stamp(current), _stamp(current), task_id),
            )
            return records

    def list_task_jobs(self, task_id: str) -> list[InventoryJobRecord]:
        with self._lock:
            task = self._connection.execute(
                "SELECT job_snapshot_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise RecordNotFound(f"task not found: {task_id}")
            rows = self._connection.execute(
                """
                SELECT job_snapshot FROM task_jobs
                WHERE task_id = ?
                ORDER BY snapshot_order
                """,
                (task_id,),
            ).fetchall()
        return [self._job_from_snapshot(row["job_snapshot"]) for row in rows]

    def upsert_job_review(
        self,
        review: JobReviewInput,
        *,
        now: datetime | None = None,
    ) -> ReviewCheckpointResult:
        validate_review_decision(review.decision)
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM job_reviews WHERE task_id = ? AND job_id = ?",
                (review.task_id, review.job_id),
            ).fetchone()
            if existing is None:
                review_id = review.review_id or _id()
                connection.execute(
                    """
                    INSERT INTO job_reviews (
                        id, task_id, job_id, context_hash, evidence_hash,
                        decision, score, reason, provider, model, matched_skills,
                        concerns, raw_response, created_at, updated_at,
                        checkpointed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        review.task_id,
                        review.job_id,
                        review.context_hash,
                        review.evidence_hash,
                        review.decision,
                        review.score,
                        review.reason,
                        review.provider,
                        review.model,
                        _dump(list(review.matched_skills)),
                        _dump(list(review.concerns)),
                        _dump(review.raw_response),
                        _stamp(current),
                        _stamp(current),
                        _stamp(current),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM job_reviews WHERE id = ?", (review_id,)
                ).fetchone()
                return ReviewCheckpointResult(self._review(row), True)

            if (
                existing["context_hash"] != review.context_hash
                or existing["evidence_hash"] != review.evidence_hash
            ):
                raise IdempotencyConflict(
                    "job review input changed for an existing task/job checkpoint"
                )
            if review.review_id and review.review_id != existing["id"]:
                raise IdempotencyConflict(
                    "task/job checkpoint already has a different review_id"
                )
            same = (
                existing["decision"] == review.decision
                and existing["score"] == review.score
                and existing["reason"] == review.reason
                and existing["provider"] == review.provider
                and existing["model"] == review.model
                and _load_tuple(existing["matched_skills"]) == review.matched_skills
                and _load_tuple(existing["concerns"]) == review.concerns
                and _load_object(existing["raw_response"]) == review.raw_response
            )
            if same:
                return ReviewCheckpointResult(self._review(existing), False)
            if existing["decision"] in FINAL_REVIEW_DECISIONS:
                raise IdempotencyConflict(
                    "a final accepted/rejected review checkpoint is immutable"
                )
            connection.execute(
                """
                UPDATE job_reviews
                SET decision = ?, score = ?, reason = ?, provider = ?, model = ?,
                    matched_skills = ?, concerns = ?, raw_response = ?,
                    attempt_count = attempt_count + 1, updated_at = ?,
                    checkpointed_at = ?
                WHERE id = ?
                """,
                (
                    review.decision,
                    review.score,
                    review.reason,
                    review.provider,
                    review.model,
                    _dump(list(review.matched_skills)),
                    _dump(list(review.concerns)),
                    _dump(review.raw_response),
                    _stamp(current),
                    _stamp(current),
                    existing["id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM job_reviews WHERE id = ?", (existing["id"],)
            ).fetchone()
            return ReviewCheckpointResult(self._review(updated), True)

    def list_job_reviews(self, task_id: str) -> list[JobReviewRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM job_reviews
                WHERE task_id = ?
                ORDER BY checkpointed_at, id
                """,
                (task_id,),
            ).fetchall()
        return [self._review(row) for row in rows]

    def add_result_file(
        self,
        result_file: ResultFileInput,
        *,
        now: datetime | None = None,
    ) -> ResultFileRecord:
        if result_file.byte_size < 0:
            raise ValueError("result byte_size cannot be negative")
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM result_files
                WHERE task_id = ? AND idempotency_key = ?
                """,
                (result_file.task_id, result_file.idempotency_key),
            ).fetchone()
            if existing is not None:
                expected = (
                    result_file.kind,
                    result_file.bucket,
                    result_file.object_key,
                    result_file.content_type,
                    result_file.byte_size,
                    result_file.sha256,
                    result_file.storage_provider,
                )
                actual = (
                    existing["kind"],
                    existing["bucket"],
                    existing["object_key"],
                    existing["content_type"],
                    existing["byte_size"],
                    existing["sha256"],
                    existing["storage_provider"],
                )
                if actual != expected:
                    raise IdempotencyConflict(
                        "result-file idempotency key was reused for different content"
                    )
                return self._result_file(existing)
            result_id = _id()
            connection.execute(
                """
                INSERT INTO result_files (
                    id, task_id, kind, bucket, object_key, content_type,
                    byte_size, sha256, storage_provider, idempotency_key,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    result_file.task_id,
                    result_file.kind,
                    result_file.bucket,
                    result_file.object_key,
                    result_file.content_type,
                    result_file.byte_size,
                    result_file.sha256,
                    result_file.storage_provider,
                    result_file.idempotency_key,
                    _stamp(current),
                    _stamp(result_file.expires_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM result_files WHERE id = ?", (result_id,)
            ).fetchone()
            return self._result_file(row)

    def list_result_files(self, task_id: str) -> list[ResultFileRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM result_files
                WHERE task_id = ? AND deleted_at IS NULL
                ORDER BY created_at, id
                """,
                (task_id,),
            ).fetchall()
        return [self._result_file(row) for row in rows]

    def list_due_cv_objects(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[CvObjectRecord]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        current = ensure_utc(now or utc_now())
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM cv_objects
                WHERE deleted_at IS NULL AND legal_hold = 0
                  AND delete_after <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM tasks
                    WHERE tasks.cv_object_id = cv_objects.id
                      AND tasks.status NOT IN ('succeeded', 'failed', 'cancelled')
                  )
                ORDER BY delete_after, id
                LIMIT ?
                """,
                (_stamp(current), limit),
            ).fetchall()
        return [self._cv_object(row) for row in rows]

    def mark_cv_deleted(
        self,
        cv_object_id: str,
        *,
        error: str = "",
        now: datetime | None = None,
    ) -> CvObjectRecord:
        current = ensure_utc(now or utc_now())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM cv_objects WHERE id = ?", (cv_object_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"CV object not found: {cv_object_id}")
            if row["deleted_at"]:
                return self._cv_object(row)
            connection.execute(
                """
                UPDATE cv_objects
                SET deleted_at = ?, deletion_attempts = deletion_attempts + 1,
                    deletion_error = ?
                WHERE id = ?
                """,
                (
                    None if error else _stamp(current),
                    error,
                    cv_object_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM cv_objects WHERE id = ?", (cv_object_id,)
            ).fetchone()
            return self._cv_object(updated)

    @staticmethod
    def _task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            owner_id=row["owner_id"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
            status=row["status"],
            phase=row["phase"],
            country=row["country"],
            target_position=row["target_position"],
            cv_object_id=row["cv_object_id"],
            request_payload=_load_object(row["request_payload"]),
            progress=_load_object(row["progress"]),
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            available_at=_time(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_time(row["lease_expires_at"]),
            cancel_requested=bool(row["cancel_requested"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=_time(row["created_at"]),
            updated_at=_time(row["updated_at"]),
            started_at=_time(row["started_at"]),
            finished_at=_time(row["finished_at"]),
            job_snapshot_at=_time(row["job_snapshot_at"]),
        )

    @staticmethod
    def _source_run(row: sqlite3.Row) -> SourceRunRecord:
        return SourceRunRecord(
            id=row["id"],
            source=row["source"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_time(row["lease_expires_at"]),
            started_at=_time(row["started_at"]),
            finished_at=_time(row["finished_at"]),
            discovered_count=row["discovered_count"],
            upserted_count=row["upserted_count"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            metrics=_load_object(row["metrics"]),
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> InventoryJobRecord:
        return InventoryJobRecord(
            id=row["id"],
            source=row["source"],
            source_job_id=row["source_job_id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            country=row["country"],
            url=row["url"],
            description=row["description"],
            salary=row["salary"],
            job_type=row["job_type"],
            published_at=_time(row["published_at"]),
            detail_page_verified=bool(row["detail_page_verified"]),
            content_hash=row["content_hash"],
            raw_payload=_load_object(row["raw_payload"]),
            first_seen_at=_time(row["first_seen_at"]),
            last_seen_at=_time(row["last_seen_at"]),
            expires_at=_time(row["expires_at"]),
            is_active=bool(row["is_active"]),
            last_source_run_id=row["last_source_run_id"],
        )

    @staticmethod
    def _review(row: sqlite3.Row) -> JobReviewRecord:
        return JobReviewRecord(
            id=row["id"],
            task_id=row["task_id"],
            job_id=row["job_id"],
            context_hash=row["context_hash"],
            evidence_hash=row["evidence_hash"],
            decision=row["decision"],
            score=row["score"],
            reason=row["reason"],
            provider=row["provider"],
            model=row["model"],
            matched_skills=_load_tuple(row["matched_skills"]),
            concerns=_load_tuple(row["concerns"]),
            raw_response=_load_object(row["raw_response"]),
            attempt_count=row["attempt_count"],
            created_at=_time(row["created_at"]),
            updated_at=_time(row["updated_at"]),
            checkpointed_at=_time(row["checkpointed_at"]),
        )

    @staticmethod
    def _result_file(row: sqlite3.Row) -> ResultFileRecord:
        return ResultFileRecord(
            id=row["id"],
            task_id=row["task_id"],
            kind=row["kind"],
            bucket=row["bucket"],
            object_key=row["object_key"],
            content_type=row["content_type"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            storage_provider=row["storage_provider"],
            idempotency_key=row["idempotency_key"],
            created_at=_time(row["created_at"]),
            expires_at=_time(row["expires_at"]),
            deleted_at=_time(row["deleted_at"]),
        )

    @staticmethod
    def _cv_object(row: sqlite3.Row) -> CvObjectRecord:
        return CvObjectRecord(
            id=row["id"],
            owner_id=row["owner_id"],
            bucket=row["bucket"],
            object_key=row["object_key"],
            original_filename=row["original_filename"],
            content_type=row["content_type"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            storage_provider=row["storage_provider"],
            created_at=_time(row["created_at"]),
            delete_after=_time(row["delete_after"]),
            deleted_at=_time(row["deleted_at"]),
            deletion_attempts=row["deletion_attempts"],
            deletion_error=row["deletion_error"],
            legal_hold=bool(row["legal_hold"]),
        )

    @staticmethod
    def _dump_job_snapshot(record: InventoryJobRecord) -> str:
        payload = {
            "id": record.id,
            "source": record.source,
            "source_job_id": record.source_job_id,
            "title": record.title,
            "company": record.company,
            "location": record.location,
            "country": record.country,
            "url": record.url,
            "description": record.description,
            "salary": record.salary,
            "job_type": record.job_type,
            "published_at": _stamp(record.published_at) if record.published_at else None,
            "detail_page_verified": record.detail_page_verified,
            "content_hash": record.content_hash,
            "raw_payload": record.raw_payload,
            "first_seen_at": _stamp(record.first_seen_at),
            "last_seen_at": _stamp(record.last_seen_at),
            "expires_at": _stamp(record.expires_at),
            "is_active": record.is_active,
            "last_source_run_id": record.last_source_run_id,
        }
        return _dump(payload)

    @staticmethod
    def _job_from_snapshot(value: str) -> InventoryJobRecord:
        payload = _load_object(value)
        return InventoryJobRecord(
            id=str(payload["id"]),
            source=str(payload["source"]),
            source_job_id=str(payload["source_job_id"]),
            title=str(payload["title"]),
            company=str(payload["company"]),
            location=str(payload["location"]),
            country=str(payload["country"]),
            url=str(payload["url"]),
            description=str(payload["description"]),
            salary=str(payload["salary"]),
            job_type=str(payload["job_type"]),
            published_at=_time(payload.get("published_at")),
            detail_page_verified=bool(payload["detail_page_verified"]),
            content_hash=str(payload["content_hash"]),
            raw_payload=(
                payload["raw_payload"]
                if isinstance(payload.get("raw_payload"), dict)
                else {}
            ),
            first_seen_at=_time(payload["first_seen_at"]),
            last_seen_at=_time(payload["last_seen_at"]),
            expires_at=_time(payload["expires_at"]),
            is_active=bool(payload["is_active"]),
            last_source_run_id=(
                str(payload["last_source_run_id"])
                if payload.get("last_source_run_id")
                else None
            ),
        )
