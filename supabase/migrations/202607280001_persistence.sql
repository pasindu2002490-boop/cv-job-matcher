-- Durable cloud queue, shared inventory and private result metadata.
-- Apply with the Supabase CLI or SQL editor using a database-owner role.

create extension if not exists pgcrypto;

create table if not exists public.cv_objects (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    bucket text not null,
    object_key text not null,
    original_filename text not null,
    content_type text not null,
    byte_size bigint not null check (byte_size >= 0),
    sha256 text not null check (length(sha256) = 64),
    storage_provider text not null default 'supabase',
    created_at timestamptz not null default now(),
    delete_after timestamptz not null,
    deleted_at timestamptz,
    deletion_attempts integer not null default 0 check (deletion_attempts >= 0),
    deletion_error text not null default '',
    legal_hold boolean not null default false,
    unique (bucket, object_key)
);

create table if not exists public.tasks (
    id uuid primary key default gen_random_uuid(),
    owner_id uuid not null references auth.users(id) on delete cascade,
    idempotency_key text not null check (length(idempotency_key) between 1 and 200),
    request_fingerprint text not null check (length(request_fingerprint) >= 16),
    status text not null default 'queued'
        check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    phase text not null default 'queued' check (length(phase) between 1 and 80),
    country text not null,
    target_position text not null,
    cv_object_id uuid references public.cv_objects(id) on delete restrict,
    request_payload jsonb not null default '{}'::jsonb,
    progress jsonb not null default '{}'::jsonb,
    attempt_count integer not null default 0 check (attempt_count >= 0),
    max_attempts integer not null default 3 check (max_attempts between 1 and 20),
    available_at timestamptz not null default now(),
    lease_owner text not null default '',
    lease_token uuid,
    lease_expires_at timestamptz,
    cancel_requested boolean not null default false,
    error_code text not null default '',
    error_message text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    job_snapshot_at timestamptz,
    unique (owner_id, idempotency_key),
    check (
        (status = 'running' and lease_token is not null and lease_expires_at is not null)
        or status <> 'running'
    )
);

create table if not exists public.source_runs (
    id uuid primary key default gen_random_uuid(),
    source text not null,
    idempotency_key text not null,
    status text not null
        check (status in ('running', 'succeeded', 'failed', 'partial')),
    lease_owner text not null,
    lease_token uuid not null default gen_random_uuid(),
    lease_expires_at timestamptz,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    discovered_count integer not null default 0 check (discovered_count >= 0),
    upserted_count integer not null default 0 check (upserted_count >= 0),
    error_code text not null default '',
    error_message text not null default '',
    metrics jsonb not null default '{}'::jsonb,
    unique (source, idempotency_key)
);

create table if not exists public.jobs (
    id uuid primary key default gen_random_uuid(),
    source text not null,
    source_job_id text not null,
    title text not null,
    company text not null default '',
    location text not null default '',
    country text not null,
    url text not null,
    description text not null default '',
    salary text not null default '',
    job_type text not null default '',
    published_at timestamptz,
    detail_page_verified boolean not null default false,
    content_hash text not null,
    raw_payload jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    expires_at timestamptz not null,
    is_active boolean not null default true,
    last_source_run_id uuid references public.source_runs(id) on delete set null,
    unique (source, source_job_id)
);

create table if not exists public.task_jobs (
    task_id uuid not null references public.tasks(id) on delete cascade,
    job_id uuid not null references public.jobs(id) on delete restrict,
    snapshot_order integer not null check (snapshot_order >= 0),
    content_hash text not null,
    job_snapshot jsonb not null,
    snapshotted_at timestamptz not null default now(),
    primary key (task_id, job_id),
    unique (task_id, snapshot_order)
);

create table if not exists public.job_reviews (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.tasks(id) on delete cascade,
    job_id uuid not null references public.jobs(id) on delete restrict,
    context_hash text not null,
    evidence_hash text not null,
    decision text not null
        check (decision in ('accepted', 'rejected', 'review_failed')),
    score double precision,
    reason text not null default '',
    provider text not null default '',
    model text not null default '',
    matched_skills jsonb not null default '[]'::jsonb,
    concerns jsonb not null default '[]'::jsonb,
    raw_response jsonb not null default '{}'::jsonb,
    attempt_count integer not null default 1 check (attempt_count >= 1),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    checkpointed_at timestamptz not null default now(),
    unique (task_id, job_id)
);

create table if not exists public.result_files (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.tasks(id) on delete cascade,
    kind text not null,
    bucket text not null,
    object_key text not null,
    content_type text not null,
    byte_size bigint not null check (byte_size >= 0),
    sha256 text not null check (length(sha256) = 64),
    storage_provider text not null default 'supabase',
    idempotency_key text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    deleted_at timestamptz,
    unique (task_id, idempotency_key),
    unique (bucket, object_key)
);

create index if not exists tasks_claimable_idx
    on public.tasks (status, available_at, lease_expires_at, created_at);
create index if not exists tasks_owner_created_idx
    on public.tasks (owner_id, created_at desc);
create index if not exists source_runs_source_started_idx
    on public.source_runs (source, started_at desc);
create index if not exists jobs_active_country_seen_idx
    on public.jobs (country, is_active, last_seen_at desc, expires_at);
create index if not exists jobs_url_idx on public.jobs (url);
create index if not exists task_jobs_task_order_idx
    on public.task_jobs (task_id, snapshot_order);
create index if not exists job_reviews_task_decision_idx
    on public.job_reviews (task_id, decision, checkpointed_at);
create index if not exists result_files_task_kind_idx
    on public.result_files (task_id, kind, created_at desc);
create index if not exists cv_objects_retention_idx
    on public.cv_objects (delete_after)
    where deleted_at is null and legal_hold = false;

-- Atomic queue claims use row locking so two Cloud Run workers cannot receive
-- the same task. The random lease token fences an expired worker from writing.
create or replace function public.claim_task(
    p_task_id uuid,
    p_worker_id text,
    p_lease_seconds integer default 900
)
returns setof public.tasks
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_task_id uuid;
    v_token uuid := gen_random_uuid();
begin
    if p_worker_id is null or length(p_worker_id) = 0 then
        raise exception 'worker_id is required';
    end if;
    if p_lease_seconds < 10 or p_lease_seconds > 86400 then
        raise exception 'lease_seconds must be between 10 and 86400';
    end if;

    select q.id into v_task_id
    from public.tasks q
    where q.id = p_task_id
      and q.cancel_requested = false
      and q.attempt_count < q.max_attempts
      and (
        (q.status = 'queued' and q.available_at <= now())
        or (q.status = 'running' and q.lease_expires_at <= now())
      )
    for update skip locked
    limit 1;

    if v_task_id is null then
        return;
    end if;

    return query
    update public.tasks t
    set status = 'running',
        phase = 'starting',
        attempt_count = t.attempt_count + 1,
        lease_owner = p_worker_id,
        lease_token = v_token,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        started_at = coalesce(t.started_at, now()),
        finished_at = null,
        error_code = '',
        error_message = '',
        updated_at = now()
    where t.id = v_task_id
    returning t.*;
end;
$$;

create or replace function public.claim_next_task(
    p_worker_id text,
    p_lease_seconds integer default 900
)
returns setof public.tasks
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_task_id uuid;
begin
    if p_worker_id is null or length(p_worker_id) = 0 then
        raise exception 'worker_id is required';
    end if;
    if p_lease_seconds < 10 or p_lease_seconds > 86400 then
        raise exception 'lease_seconds must be between 10 and 86400';
    end if;

    select q.id into v_task_id
    from public.tasks q
    where q.cancel_requested = false
      and q.attempt_count < q.max_attempts
      and (
        (q.status = 'queued' and q.available_at <= now())
        or (q.status = 'running' and q.lease_expires_at <= now())
      )
    order by q.available_at, q.created_at
    for update skip locked
    limit 1;

    if v_task_id is null then
        return;
    end if;

    return query select * from public.claim_task(
        v_task_id, p_worker_id, p_lease_seconds
    );
end;
$$;

create or replace function public.renew_task_lease(
    p_task_id uuid,
    p_worker_id text,
    p_lease_token uuid,
    p_lease_seconds integer default 900
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_updated integer;
begin
    if p_lease_seconds < 10 or p_lease_seconds > 86400 then
        raise exception 'lease_seconds must be between 10 and 86400';
    end if;
    update public.tasks t
    set lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        updated_at = now()
    where t.id = p_task_id
      and t.status = 'running'
      and t.lease_owner = p_worker_id
      and t.lease_token = p_lease_token
      and t.lease_expires_at > now();
    get diagnostics v_updated = row_count;
    return v_updated = 1;
end;
$$;

create or replace function public.reap_expired_task_leases()
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_updated integer;
begin
    update public.tasks t
    set status = 'failed',
        phase = 'failed',
        error_code = 'lease_exhausted',
        error_message = 'Worker lease expired after maximum attempts',
        lease_owner = '',
        lease_token = null,
        lease_expires_at = null,
        finished_at = now(),
        updated_at = now()
    where t.status = 'running'
      and t.lease_expires_at <= now()
      and t.attempt_count >= t.max_attempts;
    get diagnostics v_updated = row_count;
    return v_updated;
end;
$$;

revoke all on function public.claim_task(uuid, text, integer) from public;
revoke all on function public.claim_next_task(text, integer) from public;
revoke all on function public.renew_task_lease(uuid, text, uuid, integer) from public;
revoke all on function public.reap_expired_task_leases() from public;
grant execute on function public.claim_task(uuid, text, integer) to service_role;
grant execute on function public.claim_next_task(text, integer) to service_role;
grant execute on function public.renew_task_lease(uuid, text, uuid, integer) to service_role;
grant execute on function public.reap_expired_task_leases() to service_role;

-- Direct clients may only read their own task metadata/results. All inserts,
-- queue mutation, inventory writes and object access go through the server-side
-- API using the service role.
alter table public.cv_objects enable row level security;
alter table public.tasks enable row level security;
alter table public.source_runs enable row level security;
alter table public.jobs enable row level security;
alter table public.task_jobs enable row level security;
alter table public.job_reviews enable row level security;
alter table public.result_files enable row level security;

drop policy if exists "owners read own cv metadata" on public.cv_objects;
create policy "owners read own cv metadata"
    on public.cv_objects for select to authenticated
    using (owner_id = auth.uid());

drop policy if exists "owners read own tasks" on public.tasks;
create policy "owners read own tasks"
    on public.tasks for select to authenticated
    using (owner_id = auth.uid());

drop policy if exists "owners read own task snapshots" on public.task_jobs;
create policy "owners read own task snapshots"
    on public.task_jobs for select to authenticated
    using (
        exists (
            select 1 from public.tasks t
            where t.id = task_jobs.task_id and t.owner_id = auth.uid()
        )
    );

drop policy if exists "owners read own reviews" on public.job_reviews;
create policy "owners read own reviews"
    on public.job_reviews for select to authenticated
    using (
        exists (
            select 1 from public.tasks t
            where t.id = job_reviews.task_id and t.owner_id = auth.uid()
        )
    );

drop policy if exists "owners read own result metadata" on public.result_files;
create policy "owners read own result metadata"
    on public.result_files for select to authenticated
    using (
        exists (
            select 1 from public.tasks t
            where t.id = result_files.task_id and t.owner_id = auth.uid()
        )
    );

revoke all on public.cv_objects, public.tasks, public.source_runs, public.jobs,
    public.task_jobs, public.job_reviews, public.result_files
    from anon, authenticated;
grant select on public.cv_objects, public.tasks, public.task_jobs,
    public.job_reviews, public.result_files
    to authenticated;

-- Private buckets. No storage.objects client policies are created intentionally;
-- only the server-side service role can upload/download/delete CVs and reports.
insert into storage.buckets (
    id, name, public, file_size_limit, allowed_mime_types
) values (
    'cv-uploads',
    'cv-uploads',
    false,
    10485760,
    array[
        'application/pdf',
        'text/plain',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ]
)
on conflict (id) do update set
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

insert into storage.buckets (
    id, name, public, file_size_limit, allowed_mime_types
) values (
    'job-results',
    'job-results',
    false,
    52428800,
    array['text/csv', 'application/zip', 'application/json']
)
on conflict (id) do update set
    public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;
