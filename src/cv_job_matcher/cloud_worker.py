from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from .cloud_api import _build_object_store
from .inventory_sources import collect_inventory, default_inventory_sources
from .models import Job
from .persistence.checkpoint import DatabaseReviewCheckpointStore
from .persistence.factory import create_repository
from .persistence.repository import ResultFileInput
from .resend_mailer import EmailDeliveryError, ResendSettings, send_results_via_resend
from .shared_matcher import SharedInventoryMatchOptions, run_shared_inventory_match

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cloud Run worker for crawling or matching.")
    parser.add_argument("mode", choices={"crawl", "match", "cleanup"})
    args = parser.parse_args(argv)

    repository = create_repository()
    object_store = _build_object_store()
    if args.mode == "crawl":
        _crawl_inventory(repository)
    elif args.mode == "match":
        _drain_queue(repository, object_store)
    else:
        _cleanup_expired_cv_objects(repository, object_store)
    return 0


def _crawl_inventory(repository) -> None:
    worker_id = f"crawler-{os.getenv('HOSTNAME', 'local')}"
    for batch in collect_inventory(default_inventory_sources()):
        run_key = f"{batch.source}:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M')}"
        source_run = repository.begin_source_run(
            source=batch.source,
            idempotency_key=run_key,
            worker_id=worker_id,
        )
        try:
            jobs = [_to_inventory_job(job) for job in batch.jobs]
            repository.upsert_jobs(source_run.id, jobs)
            repository.finish_source_run(
                source_run.id,
                worker_id=worker_id,
                lease_token=source_run.lease_token,
                status="succeeded" if not batch.error else "partial",
                discovered_count=len(batch.jobs),
                upserted_count=len(jobs),
                metrics={"elapsed_seconds": batch.elapsed_seconds},
                error_code="" if not batch.error else "source_error",
                error_message=batch.error,
            )
        except Exception as exc:
            repository.finish_source_run(
                source_run.id,
                worker_id=worker_id,
                lease_token=source_run.lease_token,
                status="failed",
                discovered_count=len(batch.jobs),
                upserted_count=0,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )


def _drain_queue(repository, object_store) -> None:
    worker_id = f"matcher-{os.getenv('HOSTNAME', 'local')}"
    while True:
        claim = repository.claim_next_task(worker_id=worker_id)
        if claim is None:
            break
        task = claim.task
        try:
            repository.update_task(task.id, phase="starting", progress={"message": "Preparing task."})
            cv_object = repository.get_cv_object(task.cv_object_id) if task.cv_object_id else None
            if cv_object is None:
                raise RuntimeError("CV object is missing for this task")
            with tempfile.TemporaryDirectory(prefix=f"cv-task-{task.id}-") as temp_dir:
                temp_root = Path(temp_dir)
                cv_path = temp_root / cv_object.original_filename
                cv_path.write_bytes(object_store.download(cv_object.bucket, cv_object.object_key))
                out_dir = temp_root / "output"
                out_dir.mkdir(parents=True, exist_ok=True)
                job_rows = repository.list_active_jobs(country=task.country)
                summary = run_shared_inventory_match(
                    SharedInventoryMatchOptions(
                        cv_path=cv_path,
                        jobs=[_job_from_inventory(record) for record in job_rows],
                        country=task.country,
                        position=task.target_position,
                        experience_years=_experience_years(task.request_payload),
                        out_dir=out_dir,
                        include_remote_global=bool(task.request_payload.get("remote")),
                        llm_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
                        llm_provider="groq",
                        llm_batch_size=int(os.getenv("LLM_BATCH_SIZE", "5")),
                    ),
                    checkpoint_store=DatabaseReviewCheckpointStore(repository, task.id),
                )
                _record_results(repository, object_store, task.id, out_dir)
                _send_results_email(task, summary)
                repository.complete_task(
                    task.id,
                    worker_id=worker_id,
                    lease_token=claim.lease_token,
                    progress={"message": "Task complete."},
                )
        except Exception as exc:
            repository.fail_task(
                task.id,
                worker_id=worker_id,
                lease_token=claim.lease_token,
                error_code=type(exc).__name__,
                error_message=str(exc),
                retryable=True,
            )
        finally:
            _cleanup_expired_cv_objects(repository, object_store)


def _record_results(repository, object_store, task_id: str, out_dir: Path) -> None:
    bucket = os.getenv("RESULT_BUCKET", "task-results")
    for path in sorted(out_dir.glob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        object_key = f"tasks/{task_id}/results/{path.name}"
        object_store.upload(
            bucket,
            object_key,
            content,
            content_type=_content_type(path),
            overwrite=True,
        )
        repository.add_result_file(
            ResultFileInput(
                task_id=task_id,
                kind=path.stem,
                bucket=bucket,
                object_key=object_key,
                content_type=_content_type(path),
                byte_size=len(content),
                sha256=sha256,
                storage_provider=object_store.provider_name,
                idempotency_key=f"{task_id}:{path.name}:{sha256}",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
        )


def _send_results_email(task, summary) -> None:
    recipient = str(task.request_payload.get("recipient_email") or task.request_payload.get("email") or "").strip()
    if not recipient:
        return
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("RESEND_FROM", "").strip()
    if api_key and sender:
        try:
            send_results_via_resend(
                recipient,
                summary,
                task_id=task.id,
                settings=ResendSettings(
                    api_key=api_key,
                    sender=sender,
                    reply_to=os.getenv("RESEND_REPLY_TO", ""),
                ),
            )
            return
        except EmailDeliveryError:
            pass


def _cleanup_expired_cv_objects(repository, object_store) -> None:
    for record in repository.list_due_cv_objects():
        try:
            object_store.delete(record.bucket, record.object_key)
            repository.mark_cv_deleted(record.id)
        except Exception:
            repository.mark_cv_deleted(record.id, error="cleanup_failed")


def _to_inventory_job(job: Job):
    from .persistence.repository import JobUpsert

    return JobUpsert(
        source=job.source,
        source_job_id=job.source_id or hashlib.sha256(job.url.encode("utf-8")).hexdigest()[:16],
        title=job.title,
        company=job.company,
        location=job.location,
        country=job.country_hint or "sri lanka",
        url=job.url,
        description=job.description,
        content_hash=hashlib.sha256(
            f"{job.title}|{job.company}|{job.url}|{job.description}".encode("utf-8")
        ).hexdigest(),
        published_at=datetime.fromisoformat(job.published_at) if job.published_at else None,
        salary=job.salary,
        job_type=job.job_type,
        detail_page_verified=job.detail_page_verified,
        raw_payload={"source": job.source, "source_id": job.source_id},
    )


def _job_from_inventory(record):
    return Job(
        source=record.source,
        source_id=record.source_job_id,
        title=record.title,
        company=record.company,
        location=record.location,
        country_hint=record.country,
        url=record.url,
        description=record.description,
        published_at=record.published_at.isoformat() if record.published_at else "",
        salary=record.salary,
        job_type=record.job_type,
        detail_page_verified=record.detail_page_verified,
    )


def _experience_years(payload: dict[str, object]) -> float | None:
    value = payload.get("years_experience")
    if value is None:
        value = payload.get("experience_years")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".csv":
        return "text/csv"
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    if path.suffix.lower() == ".json":
        return "application/json"
    return "text/plain"
