from pathlib import Path

from cv_job_matcher.mailer import build_results_message
from cv_job_matcher.runner import RunSummary
from cv_job_matcher.web import _validate_submission, create_app


def test_results_email_attaches_all_csv_files(tmp_path: Path) -> None:
    (tmp_path / "job_matches.csv").write_text("title\nAI Engineer\n", encoding="utf-8")
    (tmp_path / "all_discovered_jobs.csv").write_text("title\nAI Engineer\n", encoding="utf-8")
    (tmp_path / "related_vacancies.csv").write_text("title\nAI Engineer\n", encoding="utf-8")
    (tmp_path / "rejected_vacancies.csv").write_text("title\nRejected\n", encoding="utf-8")
    (tmp_path / "manual_review_vacancies.csv").write_text(
        "title\nManual review\n",
        encoding="utf-8",
    )
    (tmp_path / "source_coverage.csv").write_text("source,status\nTest,completed\n", encoding="utf-8")
    (tmp_path / "companies_hiring.csv").write_text("company\nLegacy\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not attached", encoding="utf-8")
    summary = RunSummary("sri lanka", "Test User", 10, 2, 0, tmp_path)

    message = build_results_message("person@example.com", summary)

    filenames = sorted(part.get_filename() for part in message.iter_attachments())
    assert filenames == [
        "all_discovered_jobs.csv",
        "job_matches.csv",
        "manual_review_vacancies.csv",
        "rejected_vacancies.csv",
        "related_vacancies.csv",
        "source_coverage.csv",
    ]
    assert message["To"] == "person@example.com"


def test_web_form_rejects_missing_cv(tmp_path: Path) -> None:
    app = create_app({
        "TESTING": True,
        "UPLOAD_ROOT": tmp_path / "uploads",
        "OUTPUT_ROOT": tmp_path / "results",
    })

    response = app.test_client().post("/submit", data={
        "email": "person@example.com",
        "country": "Sri Lanka",
        "position": "AI Engineer",
        "experience_years": "2",
    })

    assert response.status_code == 400
    assert b"Please select a CV file" in response.data


def test_health_endpoint(tmp_path: Path) -> None:
    app = create_app({
        "TESTING": True,
        "UPLOAD_ROOT": tmp_path / "uploads",
        "OUTPUT_ROOT": tmp_path / "results",
    })

    response = app.test_client().get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["architecture"] == "concurrent-source-fan-out/single-final-llm"
    assert payload["llm_strategy"] == "groq-first/ollama-fallback"
    assert payload["configured_source_agents"] >= 40
    assert isinstance(payload["crawl4ai_enabled"], bool)
    assert isinstance(payload["groq_configured"], bool)
    assert isinstance(payload["ollama_fallback_enabled"], bool)
    assert isinstance(payload["ollama_reachable"], bool)
    assert isinstance(payload["ollama_model_available"], bool)
    assert isinstance(payload["llm_configured"], bool)
    assert isinstance(payload["smtp_configured"], bool)


def test_web_form_rejects_invalid_email(tmp_path: Path) -> None:
    app = create_app({
        "TESTING": True,
        "UPLOAD_ROOT": tmp_path / "uploads",
        "OUTPUT_ROOT": tmp_path / "results",
    })
    response = app.test_client().post(
        "/submit",
        data={
            "cv": (Path(__file__).open("rb"), "cv.txt"),
            "email": "not-an-email",
            "country": "Sri Lanka",
            "position": "AI Engineer",
            "experience_years": "2",
        },
    )

    assert response.status_code == 400
    assert b"valid email address" in response.data


def test_valid_submission_fields_pass_validation() -> None:
    class Upload:
        filename = "candidate.pdf"

    assert _validate_submission(Upload(), "person@example.com", "Sri Lanka", "DevOps Engineer", "1") == ""
