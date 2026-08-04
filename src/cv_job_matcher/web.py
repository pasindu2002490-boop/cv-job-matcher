from __future__ import annotations

import json
import os
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from .job_sources import default_providers
from .llm_filter import warm_ollama_fallback
from .mailer import send_results_email
from .runner import RunOptions, run_match

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TASKS: dict[str, dict[str, object]] = {}
TASK_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("WEB_WORKERS", "2"))))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CV_UPLOAD_MB", "10")) * 1024 * 1024,
        UPLOAD_ROOT=Path(os.getenv("UPLOAD_ROOT", "web_data/uploads")),
        OUTPUT_ROOT=Path(os.getenv("OUTPUT_ROOT", "web_data/results")),
    )
    if test_config:
        app.config.update(test_config)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        ollama_reachable, ollama_model_available = _ollama_runtime_status()
        openai_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
        groq_configured = bool(os.getenv("GROQ_API_KEY", "").strip())
        llm_provider = (
            os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto"
        )
        return jsonify(
            {
                "status": "ok",
                "architecture": "concurrent-source-fan-out/single-final-llm",
                "llm_strategy": llm_provider,
                "llm_provider": llm_provider,
                "configured_source_agents": len(default_providers()),
                "crawl4ai_enabled": os.getenv("CRAWL4AI_ENABLED", "").lower()
                in {"1", "true", "yes"},
                "openai_configured": openai_configured,
                "groq_configured": groq_configured,
                "ollama_fallback_enabled": _environment_flag(
                    "OLLAMA_FALLBACK_ENABLED", True
                ),
                "ollama_reachable": ollama_reachable,
                "ollama_model_available": ollama_model_available,
                "llm_configured": (
                    openai_configured
                    or groq_configured
                    or ollama_model_available
                ),
                "smtp_configured": _smtp_configured(),
            }
        )

    @app.post("/submit")
    def submit():
        upload = request.files.get("cv")
        email = request.form.get("email", "").strip()
        country = request.form.get("country", "").strip()
        position = request.form.get("position", "").strip()
        experience_raw = request.form.get("experience_years", "").strip()

        error = _validate_submission(upload, email, country, position, experience_raw)
        if error:
            return render_template("index.html", error=error), 400
        if not _smtp_configured():
            return render_template(
                "index.html",
                error=(
                    "Email delivery is not configured: "
                    "Email is not configured. Set SMTP_HOST and SMTP_FROM."
                ),
            ), 503
        if not _llm_configured():
            return render_template(
                "index.html",
                error=(
                    "Final LLM review is not configured. Set OPENAI_API_KEY or "
                    "GROQ_API_KEY, or enable Ollama and install the configured "
                    "local model."
                ),
            ), 503

        task_id = uuid4().hex
        extension = Path(secure_filename(upload.filename or "cv")).suffix.lower()
        upload_dir = app.config["UPLOAD_ROOT"] / task_id
        output_dir = app.config["OUTPUT_ROOT"] / task_id
        upload_dir.mkdir(parents=True, exist_ok=False)
        cv_path = upload_dir / f"cv{extension}"
        upload.save(cv_path)

        with TASK_LOCK:
            TASKS[task_id] = {
                "status": "queued",
                "message": "Your CV is queued for processing.",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

        logger.info(
            "Submission %s queued: recipient=%s position=%s country=%s experience=%s",
            task_id,
            _masked_email(email),
            position,
            country,
            experience_raw,
        )

        EXECUTOR.submit(
            _process_submission,
            task_id,
            cv_path,
            output_dir,
            email,
            country,
            position,
            float(experience_raw),
            request.form.get("include_remote_global") == "on",
            request.form.get("web_discovery") == "on",
        )
        return render_template("submitted.html", task_id=task_id, email=email), 202

    @app.get("/status/<task_id>")
    def status(task_id: str):
        with TASK_LOCK:
            task = TASKS.get(task_id)
            return (jsonify(task), 200) if task else (jsonify({"error": "Unknown task"}), 404)

    return app


def _validate_submission(upload, email: str, country: str, position: str, experience_raw: str) -> str:
    if upload is None or not upload.filename:
        return "Please select a CV file."
    if Path(secure_filename(upload.filename)).suffix.lower() not in ALLOWED_EXTENSIONS:
        return "CV must be a PDF, DOCX, TXT, or Markdown file."
    if not EMAIL_PATTERN.fullmatch(email):
        return "Enter a valid email address."
    if not country:
        return "Enter the country where you want to work."
    if not position:
        return "Enter your target position."
    try:
        experience = float(experience_raw)
    except ValueError:
        return "Experience must be a number."
    if experience < 0 or experience > 60:
        return "Experience must be between 0 and 60 years."
    return ""


def _masked_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def _set_task(task_id: str, **values: object) -> None:
    with TASK_LOCK:
        TASKS[task_id].update(values)


def _process_submission(
    task_id: str,
    cv_path: Path,
    output_dir: Path,
    email: str,
    country: str,
    position: str,
    experience_years: float,
    include_remote_global: bool,
    web_discovery: bool,
) -> None:
    try:
        logger.info("Submission %s started", task_id)
        _set_task(task_id, status="running", message="Searching and matching live jobs.")
        if _environment_flag("OLLAMA_FALLBACK_ENABLED", True):
            threading.Thread(
                target=warm_ollama_fallback,
                name=f"ollama-warm-{task_id[:8]}",
                daemon=True,
            ).start()
        llm_provider, llm_model = _resolve_web_llm()
        summary = run_match(RunOptions(
            cv_path=cv_path,
            country=country,
            position=position,
            experience_years=experience_years,
            out_dir=output_dir,
            include_remote_global=include_remote_global,
            web_discovery=web_discovery,
            llm_filter=True,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_limit=int(os.getenv("LLM_LIMIT", "500")),
            llm_strict=True,
            llm_batch_size=int(os.getenv("LLM_BATCH_SIZE", "5")),
            limit_per_source=int(os.getenv("SOURCE_RESULT_LIMIT", "5000")),
        ))
        _set_task(task_id, status="emailing", message="Preparing and sending your CSV reports.")
        send_results_email(email, summary)
        _set_task(
            task_id,
            status="complete",
            message=(
                f"Email sent with {summary.matches_written} final matches from "
                f"{summary.related_jobs} related vacancies "
                f"({summary.jobs_fetched} raw jobs discovered"
                + (
                    f", {summary.manual_review_jobs} need manual review"
                    if summary.manual_review_jobs
                    else ""
                )
                + ")."
            ),
            jobs_fetched=summary.jobs_fetched,
            related=summary.related_jobs,
            rejected=summary.rejected_jobs,
            manual_review=summary.manual_review_jobs,
            matches=summary.matches_written,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("Submission %s completed successfully", task_id)
    except Exception as exc:
        logger.exception("Submission %s failed: %s", task_id, exc)
        _set_task(task_id, status="failed", message=str(exc))
    finally:
        try:
            cv_path.unlink(missing_ok=True)
            cv_path.parent.rmdir()
        except OSError:
            pass


def _smtp_configured() -> bool:
    return bool(
        os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip()
    )


def _llm_configured() -> bool:
    if os.getenv("OPENAI_API_KEY", "").strip():
        return True
    if os.getenv("GROQ_API_KEY", "").strip():
        return True
    _, ollama_model_available = _ollama_runtime_status()
    return ollama_model_available


def _resolve_web_llm() -> tuple[str, str]:
    provider = (os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto")
    if provider == "openai" or (
        provider == "auto" and os.getenv("OPENAI_API_KEY", "").strip()
    ):
        return (
            "openai",
            os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        )
    if provider == "groq" or (
        provider == "auto" and os.getenv("GROQ_API_KEY", "").strip()
    ):
        return (
            "groq",
            os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
            or "openai/gpt-oss-20b",
        )
    return provider, os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"


def _environment_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _ollama_runtime_status() -> tuple[bool, bool]:
    if not _environment_flag("OLLAMA_FALLBACK_ENABLED", True):
        return False, False
    endpoint = (
        os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip()
        or "http://127.0.0.1:11434"
    ).rstrip("/")
    if endpoint.endswith("/api"):
        tags_url = f"{endpoint}/tags"
    elif endpoint.endswith("/v1"):
        tags_url = f"{endpoint[:-3].rstrip('/')}/api/tags"
    else:
        tags_url = f"{endpoint}/api/tags"
    try:
        request_object = Request(
            tags_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "cv-job-matcher/0.1",
            },
        )
        with urlopen(request_object, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return False, False
    configured_model = (
        os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip() or "llama3.1:8b"
    )
    model_rows = payload.get("models", []) if isinstance(payload, dict) else []
    available_models = {
        str(row.get("name", "")).strip()
        for row in model_rows
        if isinstance(row, dict)
    }
    return True, configured_model in available_models


app = create_app()


if __name__ == "__main__":
    app.run(host=os.getenv("WEB_HOST", "127.0.0.1"), port=int(os.getenv("WEB_PORT", "8000")))
