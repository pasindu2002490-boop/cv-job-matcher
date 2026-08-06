from pathlib import Path

from cv_job_matcher.mailer import build_results_message
from cv_job_matcher.resend_mailer import _RESULT_ATTACHMENTS
from cv_job_matcher.runner import RunSummary
from cv_job_matcher.web import _validate_submission, create_app


def test_results_email_attaches_only_requested_csv_files(tmp_path: Path) -> None:
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
        "related_vacancies.csv",
    ]
    assert message["To"] == "person@example.com"
    assert sorted(_RESULT_ATTACHMENTS) == filenames


def _make_app(tmp_path: Path):
    return create_app({
        "TESTING": True,
        "UPLOAD_ROOT": tmp_path / "uploads",
        "OUTPUT_ROOT": tmp_path / "results",
        "AUTH_DB_PATH": tmp_path / "auth.sqlite3",
        "SECRET_KEY": "test-secret",
    })


def _register(client, email: str = "person@example.com", password: str = "secret123") -> None:
    assert client.post(
        "/register",
        data={
            "display_name": "Test User",
            "email": email,
            "password": password,
            "password_confirm": password,
        },
        follow_redirects=False,
    ).status_code in {302, 303}


def _register_and_subscribe(client, email: str = "person@example.com", password: str = "secret123") -> None:
    _register(client, email=email, password=password)
    from cv_job_matcher.auth_store import AuthStore

    auth_db = Path(client.application.config["AUTH_DB_PATH"])
    store = AuthStore(auth_db)
    user = store.get_user_by_email(email)
    assert user is not None
    pending = store.create_payment_request(
        user.id,
        amount_lkr=1899,
        payment_method="bank_transfer",
        reference="TEST",
        note="test",
    )
    assert store.activate_subscription(pending.id, days=30) is not None


def test_submit_requires_login(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    response = app.test_client().post("/submit", data={
        "email": "person@example.com",
        "country": "Sri Lanka",
        "position": "AI Engineer",
        "experience_years": "2",
    })
    assert response.status_code in {302, 303}
    assert "/login" in response.headers.get("Location", "")


def test_new_user_gets_free_matches_without_subscribe(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    client = app.test_client()
    _register(client)
    home = client.get("/app")
    assert home.status_code == 200
    assert b"free" in home.data.lower()
    response = client.post("/submit", data={
        "email": "person@example.com",
        "country": "Sri Lanka",
        "position": "AI Engineer",
        "experience_years": "2",
    })
    assert response.status_code == 400
    assert b"Please select a CV file" in response.data


def test_home_and_pricing_pages(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert b"CareerSync" in client.get("/").data
    pricing = client.get("/pricing")
    assert pricing.status_code == 200
    assert b"1 month" in pricing.data
    assert b"1 year" in pricing.data
    assert client.get("/contact").status_code == 200


def test_web_form_rejects_missing_cv(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    client = app.test_client()
    _register_and_subscribe(client)

    response = client.post("/submit", data={
        "email": "person@example.com",
        "country": "Sri Lanka",
        "position": "AI Engineer",
        "experience_years": "2",
    })

    assert response.status_code == 400
    assert b"Please select a CV file" in response.data


def test_health_endpoint(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    response = app.test_client().get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["architecture"] == "concurrent-source-fan-out/single-final-llm"
    assert payload["llm_strategy"] in {"auto", "openai", "groq"}
    assert "llm_provider" in payload
    assert payload["configured_source_agents"] >= 40
    assert isinstance(payload["crawl4ai_enabled"], bool)
    assert isinstance(payload["openai_configured"], bool)
    assert isinstance(payload["groq_configured"], bool)
    assert isinstance(payload["ollama_fallback_enabled"], bool)
    assert isinstance(payload["ollama_reachable"], bool)
    assert isinstance(payload["ollama_model_available"], bool)
    assert isinstance(payload["llm_configured"], bool)
    assert isinstance(payload["smtp_configured"], bool)
    assert payload["subscription_price_lkr"] == 1899


def test_web_form_rejects_invalid_email(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    client = app.test_client()
    _register_and_subscribe(client)

    response = client.post(
        "/submit",
        data={
            "cv": (Path(__file__).open("rb"), "cv.txt"),
            "email": "not-an-email",
            "country": "Sri Lanka",
            "position": "AI Engineer",
            "experience_years": "2",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"valid email address" in response.data


def test_valid_submission_fields_pass_validation() -> None:
    class Upload:
        filename = "candidate.pdf"

    assert _validate_submission(Upload(), "person@example.com", "Sri Lanka", "DevOps Engineer", "1") == ""


def test_feedback_saved_and_visible_in_admin(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    client = app.test_client()
    _register(client, email="admin@example.com", password="secret123")
    from cv_job_matcher.auth_store import AuthStore

    store = AuthStore(Path(app.config["AUTH_DB_PATH"]))
    admin = store.get_user_by_email("admin@example.com")
    assert admin is not None
    store.set_admin(admin.id, True)

    response = client.post(
        "/feedback",
        data={
            "name": "Alex",
            "email": "alex@example.com",
            "rating": "5",
            "message": "Great matcher experience.",
        },
    )
    assert response.status_code == 200
    assert b"Thanks for your feedback" in response.data

    items = store.list_feedback()
    assert len(items) == 1
    assert items[0].name == "Alex"
    assert items[0].rating == 5
    assert items[0].status == "new"

    client.get("/logout", follow_redirects=True)
    client.post("/login", data={"email": "admin@example.com", "password": "secret123"})
    admin_page = client.get("/admin/feedback")
    assert admin_page.status_code == 200
    assert b"Great matcher experience." in admin_page.data
    assert b"Alex" in admin_page.data

    review = client.post(f"/admin/feedback/{items[0].id}/review", follow_redirects=True)
    assert review.status_code == 200
    assert store.list_feedback()[0].status == "reviewed"
