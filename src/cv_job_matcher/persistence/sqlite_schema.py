"""SQLite schema used for local development and repository contract tests.

Production PostgreSQL/Supabase migrations live in ``supabase/migrations``.
"""

SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cv_objects (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    storage_provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delete_after TEXT NOT NULL,
    deleted_at TEXT,
    deletion_attempts INTEGER NOT NULL DEFAULT 0 CHECK (deletion_attempts >= 0),
    deletion_error TEXT NOT NULL DEFAULT '',
    legal_hold INTEGER NOT NULL DEFAULT 0 CHECK (legal_hold IN (0, 1)),
    UNIQUE (bucket, object_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    phase TEXT NOT NULL DEFAULT 'queued',
    country TEXT NOT NULL,
    target_position TEXT NOT NULL,
    cv_object_id TEXT REFERENCES cv_objects(id) ON DELETE RESTRICT,
    request_payload TEXT NOT NULL DEFAULT '{}',
    progress TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    job_snapshot_at TEXT,
    UNIQUE (owner_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS tasks_claimable_idx
    ON tasks (status, available_at, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS tasks_owner_created_idx
    ON tasks (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS source_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
    lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_expires_at TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    upserted_count INTEGER NOT NULL DEFAULT 0 CHECK (upserted_count >= 0),
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    metrics TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, idempotency_key)
);

CREATE INDEX IF NOT EXISTS source_runs_source_started_idx
    ON source_runs (source, started_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    salary TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    detail_page_verified INTEGER NOT NULL DEFAULT 0
        CHECK (detail_page_verified IN (0, 1)),
    content_hash TEXT NOT NULL,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_source_run_id TEXT REFERENCES source_runs(id) ON DELETE SET NULL,
    UNIQUE (source, source_job_id)
);

CREATE INDEX IF NOT EXISTS jobs_active_country_seen_idx
    ON jobs (country, is_active, last_seen_at DESC, expires_at);
CREATE INDEX IF NOT EXISTS jobs_url_idx ON jobs (url);

CREATE TABLE IF NOT EXISTS task_jobs (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    snapshot_order INTEGER NOT NULL CHECK (snapshot_order >= 0),
    content_hash TEXT NOT NULL,
    job_snapshot TEXT NOT NULL,
    snapshotted_at TEXT NOT NULL,
    PRIMARY KEY (task_id, job_id),
    UNIQUE (task_id, snapshot_order)
);

CREATE INDEX IF NOT EXISTS task_jobs_task_order_idx
    ON task_jobs (task_id, snapshot_order);

CREATE TABLE IF NOT EXISTS job_reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    context_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    decision TEXT NOT NULL
        CHECK (decision IN ('accepted', 'rejected', 'review_failed')),
    score REAL,
    reason TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    matched_skills TEXT NOT NULL DEFAULT '[]',
    concerns TEXT NOT NULL DEFAULT '[]',
    raw_response TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    checkpointed_at TEXT NOT NULL,
    UNIQUE (task_id, job_id)
);

CREATE INDEX IF NOT EXISTS job_reviews_task_decision_idx
    ON job_reviews (task_id, decision, checkpointed_at);

CREATE TABLE IF NOT EXISTS result_files (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    storage_provider TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE (task_id, idempotency_key),
    UNIQUE (bucket, object_key)
);

CREATE INDEX IF NOT EXISTS result_files_task_kind_idx
    ON result_files (task_id, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS cv_objects_retention_idx
    ON cv_objects (delete_after)
    WHERE deleted_at IS NULL AND legal_hold = 0;
"""
