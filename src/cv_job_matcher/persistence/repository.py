from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence


JSONDict = dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class PersistenceError(RuntimeError):
    """Base class for durable queue/repository failures."""


class IdempotencyConflict(PersistenceError):
    """The same idempotency key was reused for different input."""


class LeaseConflict(PersistenceError):
    """A worker attempted to mutate work without owning its current lease."""


class InvalidTransition(PersistenceError):
    """A requested state transition is not valid for the current row."""


class RecordNotFound(PersistenceError):
    """The requested persistent record does not exist."""


@dataclass(frozen=True)
class CvObjectInput:
    owner_id: str
    bucket: str
    object_key: str
    original_filename: str
    content_type: str
    byte_size: int
    sha256: str
    delete_after: datetime
    storage_provider: str = "supabase"


@dataclass(frozen=True)
class CvObjectRecord:
    id: str
    owner_id: str
    bucket: str
    object_key: str
    original_filename: str
    content_type: str
    byte_size: int
    sha256: str
    storage_provider: str
    created_at: datetime
    delete_after: datetime
    deleted_at: datetime | None
    deletion_attempts: int
    deletion_error: str
    legal_hold: bool


@dataclass(frozen=True)
class TaskRecord:
    id: str
    owner_id: str
    idempotency_key: str
    request_fingerprint: str
    status: str
    phase: str
    country: str
    target_position: str
    cv_object_id: str | None
    request_payload: JSONDict
    progress: JSONDict
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime | None
    cancel_requested: bool
    error_code: str
    error_message: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    job_snapshot_at: datetime | None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class CreateTaskResult:
    task: TaskRecord
    created: bool


@dataclass(frozen=True)
class TaskClaim:
    task: TaskRecord
    lease_token: str


@dataclass(frozen=True)
class JobUpsert:
    source: str
    source_job_id: str
    title: str
    company: str
    location: str
    country: str
    url: str
    description: str
    content_hash: str
    published_at: datetime | None = None
    salary: str = ""
    job_type: str = ""
    detail_page_verified: bool = False
    raw_payload: JSONDict = field(default_factory=dict)


@dataclass(frozen=True)
class InventoryJobRecord:
    id: str
    source: str
    source_job_id: str
    title: str
    company: str
    location: str
    country: str
    url: str
    description: str
    salary: str
    job_type: str
    published_at: datetime | None
    detail_page_verified: bool
    content_hash: str
    raw_payload: JSONDict
    first_seen_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_active: bool
    last_source_run_id: str | None


@dataclass(frozen=True)
class SourceRunRecord:
    id: str
    source: str
    idempotency_key: str
    status: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    discovered_count: int
    upserted_count: int
    error_code: str
    error_message: str
    metrics: JSONDict


@dataclass(frozen=True)
class JobReviewInput:
    task_id: str
    job_id: str
    context_hash: str
    evidence_hash: str
    decision: str
    score: float | None
    reason: str
    provider: str
    model: str
    matched_skills: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    raw_response: JSONDict = field(default_factory=dict)
    review_id: str | None = None


@dataclass(frozen=True)
class JobReviewRecord:
    id: str
    task_id: str
    job_id: str
    context_hash: str
    evidence_hash: str
    decision: str
    score: float | None
    reason: str
    provider: str
    model: str
    matched_skills: tuple[str, ...]
    concerns: tuple[str, ...]
    raw_response: JSONDict
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    checkpointed_at: datetime


@dataclass(frozen=True)
class ReviewCheckpointResult:
    review: JobReviewRecord
    created_or_updated: bool


@dataclass(frozen=True)
class ResultFileInput:
    task_id: str
    kind: str
    bucket: str
    object_key: str
    content_type: str
    byte_size: int
    sha256: str
    expires_at: datetime
    idempotency_key: str
    storage_provider: str = "supabase"


@dataclass(frozen=True)
class ResultFileRecord:
    id: str
    task_id: str
    kind: str
    bucket: str
    object_key: str
    content_type: str
    byte_size: int
    sha256: str
    storage_provider: str
    idempotency_key: str
    created_at: datetime
    expires_at: datetime
    deleted_at: datetime | None


class PersistenceRepository(Protocol):
    """Storage contract used by the API, crawler and matching worker."""

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
    ) -> CreateTaskResult: ...

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def get_cv_object(self, cv_object_id: str) -> CvObjectRecord | None: ...

    def update_task(
        self,
        task_id: str,
        *,
        phase: str | None = None,
        progress: Mapping[str, Any] | None = None,
        cancel_requested: bool | None = None,
        now: datetime | None = None,
    ) -> TaskRecord: ...

    def claim_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> TaskClaim | None: ...

    def claim_next_task(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> TaskClaim | None: ...

    def renew_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> bool: ...

    def complete_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        progress: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> TaskRecord: ...

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
    ) -> TaskRecord: ...

    def begin_source_run(
        self,
        *,
        source: str,
        idempotency_key: str,
        worker_id: str,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> SourceRunRecord: ...

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
    ) -> SourceRunRecord: ...

    def upsert_jobs(
        self,
        source_run_id: str,
        jobs: Iterable[JobUpsert],
        *,
        seen_at: datetime | None = None,
        ttl: timedelta = timedelta(days=2),
    ) -> list[InventoryJobRecord]: ...

    def list_active_jobs(
        self,
        *,
        country: str,
        seen_since: datetime | None = None,
        limit: int = 10_000,
        now: datetime | None = None,
    ) -> list[InventoryJobRecord]: ...

    def snapshot_active_jobs(
        self,
        task_id: str,
        *,
        country: str,
        seen_since: datetime | None = None,
        limit: int = 10_000,
        now: datetime | None = None,
    ) -> list[InventoryJobRecord]: ...

    def list_task_jobs(self, task_id: str) -> list[InventoryJobRecord]: ...

    def upsert_job_review(
        self,
        review: JobReviewInput,
        *,
        now: datetime | None = None,
    ) -> ReviewCheckpointResult: ...

    def list_job_reviews(self, task_id: str) -> list[JobReviewRecord]: ...

    def add_result_file(
        self,
        result_file: ResultFileInput,
        *,
        now: datetime | None = None,
    ) -> ResultFileRecord: ...

    def list_result_files(self, task_id: str) -> list[ResultFileRecord]: ...

    def list_due_cv_objects(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[CvObjectRecord]: ...

    def mark_cv_deleted(
        self,
        cv_object_id: str,
        *,
        error: str = "",
        now: datetime | None = None,
    ) -> CvObjectRecord: ...


FINAL_REVIEW_DECISIONS = frozenset({"accepted", "rejected"})
ALL_REVIEW_DECISIONS = FINAL_REVIEW_DECISIONS | {"review_failed"}


def validate_review_decision(decision: str) -> None:
    if decision not in ALL_REVIEW_DECISIONS:
        choices = ", ".join(sorted(ALL_REVIEW_DECISIONS))
        raise ValueError(f"decision must be one of: {choices}")


def validate_lease_seconds(lease_seconds: int) -> None:
    if not 10 <= lease_seconds <= 86_400:
        raise ValueError("lease_seconds must be between 10 and 86400")
