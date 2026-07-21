from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .mailer import MailSettings, send_results_email
from .runner import RunOptions, run_match

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
        return jsonify({"status": "ok"})

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
        try:
            MailSettings.from_environment()
        except (RuntimeError, ValueError) as exc:
            return render_template(
                "index.html",
                error=f"Email delivery is not configured: {exc}",
            ), 503
        if not os.getenv("GROQ_API_KEY", "").strip():
            return render_template(
                "index.html",
                error="Groq filtering is not configured. Set GROQ_API_KEY and restart the server.",
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
        _set_task(task_id, status="running", message="Searching and matching live jobs.")
        summary = run_match(RunOptions(
            cv_path=cv_path,
            country=country,
            position=position,
            experience_years=experience_years,
            out_dir=output_dir,
            include_remote_global=include_remote_global,
            web_discovery=web_discovery,
            llm_filter=True,
            llm_provider="groq",
            llm_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            llm_limit=int(os.getenv("LLM_LIMIT", "500")),
            llm_strict=True,
            llm_batch_size=int(os.getenv("LLM_BATCH_SIZE", "5")),
        ))
        _set_task(task_id, status="emailing", message="Preparing and sending your CSV reports.")
        send_results_email(email, summary)
        _set_task(
            task_id,
            status="complete",
            message=f"Email sent with {summary.matches_written} matches from {summary.jobs_fetched} jobs.",
            jobs_fetched=summary.jobs_fetched,
            matches=summary.matches_written,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        _set_task(task_id, status="failed", message=str(exc))
    finally:
        try:
            cv_path.unlink(missing_ok=True)
            cv_path.parent.rmdir()
        except OSError:
            pass


app = create_app()


if __name__ == "__main__":
    app.run(host=os.getenv("WEB_HOST", "127.0.0.1"), port=int(os.getenv("WEB_PORT", "8000")))
