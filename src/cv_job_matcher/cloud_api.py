from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename

from .cloud_auth import AuthenticationError, SupabaseTokenVerifier, bearer_token
from .cloud_launcher import CloudRunMatchingJobLauncher
from .cloud_storage import LocalPrivateObjectStore, SupabasePrivateObjectStore
from .persistence.factory import create_repository
from .persistence.repository import CvObjectInput

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str = ""


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CV_UPLOAD_MB", "10")) * 1024 * 1024,
        TASK_RETENTION_DAYS=int(os.getenv("TASK_RETENTION_DAYS", "7")),
        CV_BUCKET=os.getenv("CV_BUCKET", "private-cvs"),
        RESULT_BUCKET=os.getenv("RESULT_BUCKET", "task-results"),
        RESULT_URL_TTL_SECONDS=int(os.getenv("RESULT_URL_TTL_SECONDS", "86400")),
    )
    if test_config:
        app.config.update(test_config)

    repository = create_repository()
    object_store = _build_object_store()
    verifier = _build_verifier()

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "queue_backend": type(repository).__name__,
                "storage_backend": object_store.provider_name,
                "auth_configured": verifier is not None,
                "architecture": "Netlify frontend / Cloud Run API / Cloud Run crawler / Cloud Run matcher",
            }
        )

    @app.post("/v1/tasks")
    def create_task():
        auth_response = _authenticate(verifier)
        if isinstance(auth_response, tuple):
            principal, response = auth_response
            if response is not None:
                return response
        else:
            principal = auth_response

        upload = request.files.get("cv")
        country = _field("country")
        position = _field("position")
        years_experience = _field("years_experience", _field("experience_years"))
        remote = _bool_field("remote") or _bool_field("include_remote_global")
        recipient_email = principal.email or _field("email")

        if upload is None or not upload.filename:
            return jsonify({"error": "Please attach a CV file."}), 400
        if not country:
            return jsonify({"error": "Country is required."}), 400
        if not position:
            return jsonify({"error": "Target position is required."}), 400
        if not recipient_email:
            return jsonify({"error": "A recipient email address is required."}), 400
        try:
            experience = float(years_experience or "0")
        except ValueError:
            return jsonify({"error": "years_experience must be numeric."}), 400
        if experience < 0 or experience > 60:
            return jsonify({"error": "years_experience must be between 0 and 60."}), 400

        file_bytes = upload.read()
        if not file_bytes:
            return jsonify({"error": "The uploaded CV is empty."}), 400

        request_key = request.headers.get("Idempotency-Key", "").strip() or hashlib.sha256(
            f"{principal.user_id}:{country}:{position}:{experience}".encode("utf-8")
        ).hexdigest()
        sha256 = hashlib.sha256(file_bytes).hexdigest()
        task_seed = f"{principal.user_id}:{request_key}:{country}:{position}:{sha256}"
        task_id = hashlib.sha256(task_seed.encode("utf-8")).hexdigest()[:32]

        filename = secure_filename(upload.filename or "cv")
        suffix = Path(filename).suffix.lower() or ".pdf"
        bucket = app.config["CV_BUCKET"]
        object_key = f"tasks/{task_id}/cv{suffix}"
        object_store.upload(
            bucket,
            object_key,
            file_bytes,
            content_type=upload.mimetype or "application/octet-stream",
            overwrite=True,
        )

        cv_object = CvObjectInput(
            owner_id=principal.user_id,
            bucket=bucket,
            object_key=object_key,
            original_filename=filename,
            content_type=upload.mimetype or "application/octet-stream",
            byte_size=len(file_bytes),
            sha256=sha256,
            delete_after=datetime.now(timezone.utc)
            + timedelta(days=app.config["TASK_RETENTION_DAYS"]),
            storage_provider=object_store.provider_name,
        )
        request_payload = {
            "country": country,
            "position": position,
            "years_experience": experience,
            "remote": remote,
            "recipient_email": recipient_email,
            "cv_filename": filename,
        }
        create_result = repository.create_task(
            owner_id=principal.user_id,
            idempotency_key=request_key,
            request_fingerprint=_fingerprint(request_payload, sha256),
            country=country,
            target_position=position,
            request_payload=request_payload,
            cv_object=cv_object,
            task_id=task_id,
        )

        _launch_matcher_if_configured()
        return jsonify({"task": _serialize_task(create_result.task, repository, object_store)}), 202

    @app.get("/v1/tasks/<task_id>")
    def get_task(task_id: str):
        task = repository.get_task(task_id)
        if task is None:
            return jsonify({"error": "Unknown task"}), 404
        return jsonify({"task": _serialize_task(task, repository, object_store)})

    @app.get("/v1/tasks/<task_id>/results")
    def get_results(task_id: str):
        task = repository.get_task(task_id)
        if task is None:
            return jsonify({"error": "Unknown task"}), 404
        return jsonify(
            {
                "task": _serialize_task(task, repository, object_store),
                "result_files": _serialize_result_files(repository, object_store, task_id),
            }
        )

    return app


def _build_object_store():
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if supabase_url and service_role_key:
        return SupabasePrivateObjectStore(supabase_url, service_role_key)
    return LocalPrivateObjectStore(Path(os.getenv("PRIVATE_STORAGE_ROOT", "web_data/private_storage")))


def _build_verifier():
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    publishable_key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if supabase_url and publishable_key:
        return SupabaseTokenVerifier(supabase_url, publishable_key)
    return None


def _authenticate(verifier):
    if verifier is None:
        return Principal(user_id="anonymous", email=os.getenv("TASK_ANONYMOUS_EMAIL", "").strip())
    try:
        token = bearer_token(request.headers)
        principal = verifier.verify(token)
        return Principal(user_id=principal.user_id, email=principal.email)
    except AuthenticationError as exc:
        return None, (jsonify({"error": str(exc)}), 401)


def _field(name: str, fallback: str = "") -> str:
    return str(request.form.get(name, fallback) or "").strip()


def _bool_field(name: str) -> bool:
    return str(request.form.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _fingerprint(request_payload: dict[str, object], sha256: str) -> str:
    encoded = json.dumps(
        {**request_payload, "sha256": sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _serialize_task(task, repository, object_store):
    payload = {
        "id": task.id,
        "task_id": task.id,
        "status": task.phase or task.status,
        "task_status": task.status,
        "status_message": task.progress.get("message") or task.error_message or task.phase,
        "phase": task.phase,
        "country": task.country,
        "position": task.target_position,
        "progress": task.progress,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "result_files": _serialize_result_files(repository, object_store, task.id),
    }
    if task.cv_object_id:
        payload["cv_object_id"] = task.cv_object_id
    if getattr(task, "request_payload", None):
        payload["request_payload"] = task.request_payload
    return payload


def _serialize_result_files(repository, object_store, task_id: str):
    files = []
    for result in repository.list_result_files(task_id):
        if result.deleted_at is not None:
            continue
        files.append(
            {
                "id": result.id,
                "kind": result.kind,
                "filename": Path(result.object_key).name,
                "content_type": result.content_type,
                "download_url": object_store.create_signed_url(
                    result.bucket,
                    result.object_key,
                    expires_seconds=int(os.getenv("RESULT_URL_TTL_SECONDS", "86400")),
                ),
            }
        )
    return files


def _launch_matcher_if_configured() -> None:
    if not _bool_env("AUTO_LAUNCH_MATCHER", default=True):
        return
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    region = os.getenv("CLOUD_RUN_REGION", "").strip()
    job_name = os.getenv("MATCHER_JOB_NAME", "").strip()
    if not (project and region and job_name):
        return
    try:
        CloudRunMatchingJobLauncher(project, region, job_name).launch()
    except Exception:
        return


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


app = create_app()
