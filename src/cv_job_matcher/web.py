from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from dotenv import load_dotenv
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from .auth_store import AuthStore
from .job_sources import default_providers
from .llm_filter import warm_ollama_fallback
from .mailer import send_results_email, send_subscription_email, send_welcome_email
from .runner import RunOptions, run_match

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TASKS: dict[str, dict[str, object]] = {}
TASK_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("WEB_WORKERS", "2"))))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "").strip() or uuid4().hex,
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CV_UPLOAD_MB", "10")) * 1024 * 1024,
        UPLOAD_ROOT=Path(os.getenv("UPLOAD_ROOT", "web_data/uploads")),
        OUTPUT_ROOT=Path(os.getenv("OUTPUT_ROOT", "web_data/results")),
        AVATAR_ROOT=Path(os.getenv("AVATAR_ROOT", "web_data/avatars")),
        AUTH_DB_PATH=Path(os.getenv("AUTH_DB_PATH", "web_data/auth.sqlite3")),
        SUBSCRIPTION_PRICE_LKR=int(os.getenv("SUBSCRIPTION_PRICE_LKR", "1899")),
        SUBSCRIPTION_DAYS=int(os.getenv("SUBSCRIPTION_DAYS", "30")),
        PLAN_MONTHLY_PRICE_LKR=int(
            os.getenv("PLAN_MONTHLY_PRICE_LKR", os.getenv("SUBSCRIPTION_PRICE_LKR", "1899"))
        ),
        PLAN_MONTHLY_DAYS=int(os.getenv("PLAN_MONTHLY_DAYS", os.getenv("SUBSCRIPTION_DAYS", "30"))),
        PLAN_YEARLY_PRICE_LKR=int(os.getenv("PLAN_YEARLY_PRICE_LKR", "15999")),
        PLAN_YEARLY_DAYS=int(os.getenv("PLAN_YEARLY_DAYS", "365")),
        FREE_RUN_LIMIT=int(os.getenv("FREE_RUN_LIMIT", "2")),
        CONTACT_EMAIL=os.getenv("CONTACT_EMAIL", "").strip()
        or os.getenv("ADMIN_EMAIL", "").strip()
        or os.getenv("SMTP_FROM", "").strip()
        or "hello@careersync.best",
        CONTACT_PHONE=os.getenv("CONTACT_PHONE", "").strip(),
        CONTACT_WHATSAPP=os.getenv("CONTACT_WHATSAPP", "").strip(),
        CONTACT_HOURS=os.getenv("CONTACT_HOURS", "Mon–Fri, 9:00–17:00 (Sri Lanka)").strip(),
    )
    if test_config:
        app.config.update(test_config)

    auth_store = AuthStore(Path(app.config["AUTH_DB_PATH"]))
    Path(app.config["AVATAR_ROOT"]).mkdir(parents=True, exist_ok=True)
    _ensure_bootstrap_admin(auth_store)

    @app.before_request
    def load_current_user() -> None:
        g.user = None
        g.subscription = None
        user_id = session.get("user_id")
        if not user_id:
            return
        user = auth_store.get_user(user_id)
        if user is None:
            session.clear()
            return
        g.user = user
        g.subscription = auth_store.active_subscription(user.id)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            if not g.user.is_admin:
                return (
                    render_template(
                        "app.html",
                        error="Admin access required.",
                        plans=_plans(app),
                    ),
                    403,
                )
            return view(*args, **kwargs)

        return wrapped

    @app.context_processor
    def inject_globals():
        user = getattr(g, "user", None)
        subscription = getattr(g, "subscription", None)
        subscribed = subscription is not None
        free_limit = int(app.config["FREE_RUN_LIMIT"])
        free_runs_left = 0
        if user is not None and not subscribed:
            free_runs_left = auth_store.free_runs_remaining(user.id, free_limit)
        can_match = bool(user) and (subscribed or free_runs_left > 0)
        return {
            "user": user,
            "subscription": subscription,
            "subscribed": subscribed,
            "can_match": can_match,
            "free_run_limit": free_limit,
            "free_runs_left": free_runs_left,
            "price": app.config["PLAN_MONTHLY_PRICE_LKR"],
            "plans": _plans(app),
            "contact": _contact_details(app),
        }

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.get("/app")
    def matcher():
        return render_template("app.html", error=None)

    @app.get("/pricing")
    def pricing():
        return render_template("pricing.html")

    @app.get("/contact")
    def contact():
        return render_template(
            "contact.html",
            error=None,
            message=None,
            form={"name": "", "email": "", "subject": "", "message": ""},
        )

    @app.post("/contact")
    def contact_post():
        form = {
            "name": request.form.get("name", "").strip(),
            "email": request.form.get("email", "").strip(),
            "subject": request.form.get("subject", "").strip(),
            "message": request.form.get("message", "").strip(),
        }
        if not form["name"] or not form["email"] or not form["subject"] or not form["message"]:
            return render_template(
                "contact.html",
                error="Please fill in all fields.",
                message=None,
                form=form,
            ), 400
        if not EMAIL_PATTERN.fullmatch(form["email"].lower()):
            return render_template(
                "contact.html",
                error="Enter a valid email address.",
                message=None,
                form=form,
            ), 400
        destination = app.config["CONTACT_EMAIL"]
        logger.info(
            "Contact form from %s subject=%s to=%s",
            _masked_email(form["email"]),
            form["subject"][:80],
            _masked_email(destination),
        )
        sent = _send_contact_email(destination, form)
        if sent:
            message = "Thanks — your message was sent. We’ll get back to you soon."
        else:
            message = (
                f"Thanks — we received your message. You can also email us directly at {destination}."
            )
        return render_template(
            "contact.html",
            error=None,
            message=message,
            form={"name": "", "email": "", "subject": "", "message": ""},
        )

    @app.get("/feedback")
    def feedback():
        prefill_email = g.user.email if g.user else ""
        return render_template(
            "feedback.html",
            error=None,
            message=None,
            form={"name": "", "email": prefill_email, "rating": "", "message": ""},
        )

    @app.post("/feedback")
    def feedback_post():
        form = {
            "name": request.form.get("name", "").strip(),
            "email": request.form.get("email", "").strip(),
            "rating": request.form.get("rating", "").strip(),
            "message": request.form.get("message", "").strip(),
        }
        if not form["name"] or not form["email"] or not form["rating"] or not form["message"]:
            return render_template(
                "feedback.html",
                error="Please fill in all fields.",
                message=None,
                form=form,
            ), 400
        if form["rating"] not in {"1", "2", "3", "4", "5"}:
            return render_template(
                "feedback.html",
                error="Select a valid rating.",
                message=None,
                form=form,
            ), 400
        if not EMAIL_PATTERN.fullmatch(form["email"].lower()):
            return render_template(
                "feedback.html",
                error="Enter a valid email address.",
                message=None,
                form=form,
            ), 400
        destination = app.config["CONTACT_EMAIL"]
        logger.info(
            "Feedback from %s rating=%s",
            _masked_email(form["email"]),
            form["rating"],
        )
        auth_store.create_feedback(
            name=form["name"],
            email=form["email"],
            rating=int(form["rating"]),
            message=form["message"],
            user_id=g.user.id if g.user else "",
        )
        _send_contact_email(
            destination,
            {
                "name": form["name"],
                "email": form["email"],
                "subject": f"Feedback rating {form['rating']}/5",
                "message": form["message"],
            },
        )
        return render_template(
            "feedback.html",
            error=None,
            message="Thanks for your feedback — we appreciate it.",
            form={"name": "", "email": form["email"], "rating": "", "message": ""},
        )

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.get("/terms")
    def terms():
        return render_template("terms.html")

    @app.get("/refund")
    def refund():
        return render_template("refund.html")

    @app.get("/returns")
    def returns():
        return redirect(url_for("refund"))

    @app.get("/register")
    def register():
        if g.user is not None:
            return redirect(url_for("matcher"))
        return render_template("register.html", error=None)

    @app.post("/register")
    def register_post():
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        if not EMAIL_PATTERN.fullmatch(email):
            return render_template("register.html", error="Enter a valid email."), 400
        if len(display_name) < 2:
            return render_template("register.html", error="Enter your display name."), 400
        if len(password) < 8:
            return render_template(
                "register.html",
                error="Password must be at least 8 characters.",
            ), 400
        if password != confirm:
            return render_template("register.html", error="Passwords do not match."), 400
        if auth_store.get_user_by_email(email) is not None:
            return render_template(
                "register.html",
                error="An account with that email already exists.",
            ), 400
        user = auth_store.create_user(email, password, display_name=display_name)
        session["user_id"] = user.id
        logger.info("Registered user %s", _masked_email(email))
        welcome_sent = False
        try:
            welcome_sent = bool(
                send_welcome_email(email, free_run_limit=app.config["FREE_RUN_LIMIT"])
            )
        except Exception:
            logger.exception("Welcome email failed for %s", _masked_email(email))
        if not welcome_sent:
            logger.warning(
                "Welcome email was not delivered for %s (check SMTP_* env)",
                _masked_email(email),
            )
        return redirect(url_for("matcher"))

    @app.get("/login")
    def login():
        if g.user is not None:
            return redirect(url_for("matcher"))
        return render_template("login.html", error=None, message=None)

    @app.post("/login")
    def login_post():
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = auth_store.authenticate(email, password)
        if user is None:
            return render_template(
                "login.html",
                error="Invalid email or password.",
                message=None,
            ), 401
        session["user_id"] = user.id
        destination = request.args.get("next") or url_for("matcher")
        return redirect(destination)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.get("/media/avatars/<path:filename>")
    def avatar_media(filename: str):
        safe = Path(filename).name
        return send_from_directory(app.config["AVATAR_ROOT"], safe)

    @app.get("/profile")
    @login_required
    def profile():
        return render_template("profile.html", error=None, message=None)

    @app.post("/profile")
    @login_required
    def profile_post():
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        if not display_name:
            return render_template("profile.html", error="Display name is required.", message=None), 400
        if password or confirm:
            if len(password) < 8:
                return render_template(
                    "profile.html",
                    error="Password must be at least 8 characters.",
                    message=None,
                ), 400
            if password != confirm:
                return render_template(
                    "profile.html",
                    error="Passwords do not match.",
                    message=None,
                ), 400
        avatar_name = None
        upload = request.files.get("avatar")
        if upload and upload.filename:
            extension = Path(secure_filename(upload.filename)).suffix.lower()
            if extension not in AVATAR_EXTENSIONS:
                return render_template(
                    "profile.html",
                    error="Avatar must be PNG, JPG, or WebP.",
                    message=None,
                ), 400
            upload.stream.seek(0, 2)
            size = upload.stream.tell()
            upload.stream.seek(0)
            if size > 2 * 1024 * 1024:
                return render_template(
                    "profile.html",
                    error="Avatar must be 2 MB or smaller.",
                    message=None,
                ), 400
            avatar_name = f"{g.user.id}{extension}"
            destination = Path(app.config["AVATAR_ROOT"]) / avatar_name
            upload.save(destination)
        updated = auth_store.update_profile(
            g.user.id,
            display_name=display_name,
            avatar_path=avatar_name,
            password=password or None,
        )
        if updated is None:
            return render_template("profile.html", error="Could not update profile.", message=None), 400
        g.user = updated
        return render_template("profile.html", error=None, message="Profile saved.")

    @app.get("/subscribe")
    @login_required
    def subscribe():
        plan_id = request.args.get("plan", "monthly")
        return render_template(
            "subscribe.html",
            **_subscribe_context(auth_store, app=app, selected_plan=plan_id),
        )

    @app.post("/subscribe/bank")
    @login_required
    def subscribe_bank():
        plan = _resolve_plan(app, request.form.get("plan", "monthly"))
        reference = request.form.get("reference", "").strip()
        note = request.form.get("note", "").strip()
        amount_raw = request.form.get("amount", "").strip()
        price = plan["price_lkr"]
        try:
            amount = int(float(amount_raw))
        except ValueError:
            return render_template(
                "subscribe.html",
                **_subscribe_context(
                    auth_store, app=app, selected_plan=plan["id"], error="Enter a valid amount."
                ),
            ), 400
        if amount < price:
            return render_template(
                "subscribe.html",
                **_subscribe_context(
                    auth_store,
                    app=app,
                    selected_plan=plan["id"],
                    error=f"Amount must be at least LKR {price}.",
                ),
            ), 400
        if not reference:
            return render_template(
                "subscribe.html",
                **_subscribe_context(
                    auth_store,
                    app=app,
                    selected_plan=plan["id"],
                    error="Transfer reference is required.",
                ),
            ), 400
        plan_note = f"plan:{plan['id']}"
        full_note = f"{plan_note} | {note}".strip(" |") if note else plan_note
        auth_store.create_payment_request(
            g.user.id,
            amount_lkr=amount,
            payment_method="bank_transfer",
            reference=reference,
            note=full_note,
        )
        logger.info(
            "Bank payment submitted by %s reference=%s amount=%s plan=%s",
            _masked_email(g.user.email),
            reference,
            amount,
            plan["id"],
        )
        return render_template(
            "subscribe.html",
            **_subscribe_context(
                auth_store,
                app=app,
                selected_plan=plan["id"],
                message=(
                    "Payment details received. Access unlocks after admin "
                    "verification of your bank transfer."
                ),
            ),
        )

    @app.post("/subscribe/payhere/start")
    @login_required
    def subscribe_payhere_start():
        plan = _resolve_plan(app, request.form.get("plan", "monthly"))
        if not _payhere_enabled():
            return render_template(
                "subscribe.html",
                **_subscribe_context(
                    auth_store,
                    app=app,
                    selected_plan=plan["id"],
                    error="Card checkout is not configured yet (missing PayHere keys).",
                ),
            ), 503
        first_name = request.form.get("first_name", "").strip() or "Subscriber"
        last_name = request.form.get("last_name", "").strip() or "User"
        phone = request.form.get("phone", "").strip() or "0700000000"
        subscription = auth_store.create_payment_request(
            g.user.id,
            amount_lkr=plan["price_lkr"],
            payment_method="payhere",
            reference="",
            note=f"plan:{plan['id']} | Awaiting PayHere card checkout",
        )
        context = _subscribe_context(
            auth_store,
            app=app,
            selected_plan=plan["id"],
            payhere_order_id=subscription.id,
            checkout_first_name=first_name,
            checkout_last_name=last_name,
            checkout_phone=phone,
        )
        context["autosubmit_payhere"] = True
        return render_template("subscribe.html", **context)

    @app.post("/payments/payhere/notify")
    def payhere_notify():
        if not _payhere_enabled():
            return "disabled", 503
        merchant_secret = os.getenv("PAYHERE_MERCHANT_SECRET", "").strip()
        order_id = request.form.get("order_id", "").strip()
        payment_id = request.form.get("payment_id", "").strip()
        status_code = request.form.get("status_code", "").strip()
        md5sig = request.form.get("md5sig", "").strip().upper()
        amount = request.form.get("payhere_amount", "").strip()
        currency = request.form.get("payhere_currency", "").strip()
        merchant_id = request.form.get("merchant_id", "").strip()
        local = (
            hashlib.md5(
                (
                    merchant_id
                    + order_id
                    + amount
                    + currency
                    + status_code
                    + hashlib.md5(merchant_secret.encode("utf-8")).hexdigest().upper()
                ).encode("utf-8")
            )
            .hexdigest()
            .upper()
        )
        if local != md5sig:
            logger.warning("PayHere notify signature mismatch for order %s", order_id)
            return "invalid", 400
        subscription = auth_store.get_subscription(order_id)
        if subscription is None:
            return "unknown", 404
        if status_code == "2":
            days = _days_for_subscription(app, subscription)
            activated = auth_store.activate_subscription(
                order_id,
                days=days,
                payment_reference=payment_id,
            )
            logger.info(
                "PayHere activated subscription %s payment_id=%s",
                order_id,
                payment_id,
            )
            if activated is not None:
                _notify_subscription_email(app, auth_store, activated)
        return "ok", 200

    @app.get("/payments/payhere/return")
    @login_required
    def payhere_return():
        # Notify webhook may arrive slightly after the browser redirect.
        active = auth_store.active_subscription(g.user.id)
        if active is not None:
            return redirect(url_for("matcher"))
        return render_template(
            "subscribe.html",
            **_subscribe_context(
                auth_store,
                app=app,
                message=(
                    "Payment received. If access is not active yet, wait a few "
                    "seconds and refresh — card confirmation is automatic."
                ),
            ),
        )

    @app.get("/admin")
    @admin_required
    def admin_home():
        rows = []
        for item in auth_store.list_users():
            rows.append(
                {
                    "user": item,
                    "subscription": auth_store.active_subscription(item.id)
                    or auth_store.latest_subscription(item.id),
                }
            )
        return render_template(
            "admin.html",
            users=rows,
            message=None,
            error=None,
        )

    @app.post("/admin/users/<user_id>/toggle-admin")
    @admin_required
    def admin_toggle_admin(user_id: str):
        target = auth_store.get_user(user_id)
        if target is None:
            return _admin_users_page(auth_store, error="User not found."), 404
        if target.id == g.user.id and target.is_admin:
            return _admin_users_page(auth_store, error="You cannot remove your own admin role."), 400
        auth_store.set_admin(user_id, not target.is_admin)
        return _admin_users_page(auth_store, message=f"Updated admin role for {target.email}.")

    @app.post("/admin/users/<user_id>/reset-free-runs")
    @admin_required
    def admin_reset_free_runs(user_id: str):
        target = auth_store.set_free_runs_used(user_id, 0)
        if target is None:
            return _admin_users_page(auth_store, error="User not found."), 404
        return _admin_users_page(auth_store, message=f"Reset free runs for {target.email}.")

    @app.post("/admin/users/<user_id>/grant")
    @admin_required
    def admin_grant_plan(user_id: str):
        target = auth_store.get_user(user_id)
        if target is None:
            return _admin_users_page(auth_store, error="User not found."), 404
        plan = _resolve_plan(app, request.form.get("plan", "monthly"))
        pending = auth_store.create_payment_request(
            user_id,
            amount_lkr=plan["price_lkr"],
            payment_method="admin_grant",
            reference="ADMIN",
            note=f"plan:{plan['id']} | Granted by admin",
        )
        activated = auth_store.activate_subscription(pending.id, days=plan["days"])
        if activated is not None:
            _notify_subscription_email(app, auth_store, activated)
        return _admin_users_page(
            auth_store,
            message=f"Granted {plan['name']} to {target.email}.",
        )

    @app.post("/admin/users/<user_id>/delete")
    @admin_required
    def admin_delete_user(user_id: str):
        if user_id == g.user.id:
            return _admin_users_page(auth_store, error="You cannot delete your own account."), 400
        target = auth_store.get_user(user_id)
        if target is None:
            return _admin_users_page(auth_store, error="User not found."), 404
        auth_store.delete_user(user_id)
        avatar_root = Path(app.config["AVATAR_ROOT"])
        for path in avatar_root.glob(f"{user_id}.*"):
            path.unlink(missing_ok=True)
        return _admin_users_page(auth_store, message=f"Deleted {target.email}.")

    @app.get("/admin/payments")
    @admin_required
    def admin_payments():
        pending = []
        for item in auth_store.list_pending_subscriptions():
            owner = auth_store.get_user(item.user_id)
            pending.append(
                {
                    "subscription": item,
                    "email": owner.email if owner else item.user_id,
                }
            )
        return render_template(
            "admin_payments.html",
            pending=pending,
            message=None,
            error=None,
        )

    @app.post("/admin/payments/<subscription_id>/activate")
    @admin_required
    def admin_activate(subscription_id: str):
        pending_item = auth_store.get_subscription(subscription_id)
        days = _days_for_subscription(app, pending_item) if pending_item else app.config["PLAN_MONTHLY_DAYS"]
        activated = auth_store.activate_subscription(
            subscription_id,
            days=days,
        )
        if activated is None:
            return render_template(
                "admin_payments.html",
                pending=[],
                message=None,
                error="Subscription not found.",
            ), 404
        _notify_subscription_email(app, auth_store, activated)
        pending = []
        for item in auth_store.list_pending_subscriptions():
            owner = auth_store.get_user(item.user_id)
            pending.append(
                {
                    "subscription": item,
                    "email": owner.email if owner else item.user_id,
                }
            )
        return render_template(
            "admin_payments.html",
            pending=pending,
            message=f"Activated subscription {subscription_id[:8]}…",
            error=None,
        )

    @app.get("/admin/feedback")
    @admin_required
    def admin_feedback():
        return _admin_feedback_page(auth_store)

    @app.post("/admin/feedback/<feedback_id>/review")
    @admin_required
    def admin_feedback_review(feedback_id: str):
        auth_store.set_feedback_status(feedback_id, "reviewed")
        return _admin_feedback_page(auth_store, message="Marked as reviewed.")

    @app.post("/admin/feedback/<feedback_id>/delete")
    @admin_required
    def admin_feedback_delete(feedback_id: str):
        auth_store.delete_feedback(feedback_id)
        return _admin_feedback_page(auth_store, message="Feedback deleted.")

    @app.get("/health")
    def health():
        ollama_reachable, ollama_model_available = _ollama_runtime_status()
        openai_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
        groq_configured = bool(os.getenv("GROQ_API_KEY", "").strip())
        llm_provider = os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto"
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
                    openai_configured or groq_configured or ollama_model_available
                ),
                "smtp_configured": _smtp_configured(),
                "auth_enabled": True,
                "subscription_price_lkr": app.config["PLAN_MONTHLY_PRICE_LKR"],
                "plan_yearly_price_lkr": app.config["PLAN_YEARLY_PRICE_LKR"],
                "payhere_enabled": _payhere_enabled(),
                "free_run_limit": app.config["FREE_RUN_LIMIT"],
            }
        )

    @app.post("/submit")
    @login_required
    def submit():
        using_free_run = g.subscription is None
        if using_free_run:
            remaining = auth_store.free_runs_remaining(
                g.user.id, app.config["FREE_RUN_LIMIT"]
            )
            if remaining <= 0:
                return redirect(url_for("subscribe"))

        upload = request.files.get("cv")
        email = request.form.get("email", "").strip() or g.user.email
        country = request.form.get("country", "").strip()
        position = request.form.get("position", "").strip()
        experience_raw = request.form.get("experience_years", "").strip()

        error = _validate_submission(upload, email, country, position, experience_raw)
        if error:
            return render_template("app.html", error=error, plans=_plans(app)), 400
        if not _smtp_configured():
            return render_template(
                "app.html",
                error=(
                    "Email delivery is not configured: "
                    "Email is not configured. Set SMTP_HOST and SMTP_FROM."
                ),
                plans=_plans(app),
            ), 503
        if not _llm_configured():
            return render_template(
                "app.html",
                error=(
                    "Final LLM review is not configured. Set OPENAI_API_KEY or "
                    "GROQ_API_KEY, or enable Ollama and install the configured "
                    "local model."
                ),
                plans=_plans(app),
            ), 503

        if using_free_run and not auth_store.consume_free_run(
            g.user.id, app.config["FREE_RUN_LIMIT"]
        ):
            return redirect(url_for("subscribe"))

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
                "user_id": g.user.id,
            }

        logger.info(
            "Submission %s queued: user=%s recipient=%s position=%s country=%s experience=%s free_run=%s",
            task_id,
            g.user.id[:8],
            _masked_email(email),
            position,
            country,
            experience_raw,
            using_free_run,
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
    @login_required
    def status(task_id: str):
        with TASK_LOCK:
            task = TASKS.get(task_id)
            if not task:
                return jsonify({"error": "Unknown task"}), 404
            if task.get("user_id") and task.get("user_id") != g.user.id and not g.user.is_admin:
                return jsonify({"error": "Forbidden"}), 403
            return jsonify(task), 200

    return app


def _subscribe_context(
    auth_store: AuthStore,
    *,
    app: Flask,
    error: str | None = None,
    message: str | None = None,
    selected_plan: str = "monthly",
    payhere_order_id: str | None = None,
    checkout_first_name: str = "Subscriber",
    checkout_last_name: str = "User",
    checkout_phone: str = "0700000000",
) -> dict:
    user = getattr(g, "user", None)
    latest = auth_store.latest_subscription(user.id) if user else None
    active = auth_store.active_subscription(user.id) if user else None
    current_plan = _resolve_plan(app, selected_plan)
    order_id = payhere_order_id or (
        latest.id if latest and latest.status == "pending" and latest.payment_method == "payhere" else uuid4().hex
    )
    payhere = None
    if _payhere_enabled() and user:
        payhere = _payhere_form(
            user.email,
            order_id,
            amount_lkr=current_plan["price_lkr"],
            first_name=checkout_first_name,
            last_name=checkout_last_name,
            phone=checkout_phone,
        )
    paid_plans = [plan for plan in _plans(app) if plan["id"] in {"monthly", "yearly"}]
    return {
        "error": error,
        "message": message,
        "subscription": active or latest,
        "bank": _bank_details(),
        "payhere_enabled": _payhere_enabled(),
        "payhere": payhere,
        "checkout_first_name": checkout_first_name,
        "checkout_last_name": checkout_last_name,
        "checkout_phone": checkout_phone,
        "selected_plan": current_plan["id"],
        "current_plan": current_plan,
        "paid_plans": paid_plans,
        "plans": _plans(app),
    }


def _plans(app: Flask) -> list[dict]:
    monthly = app.config["PLAN_MONTHLY_PRICE_LKR"]
    yearly = app.config["PLAN_YEARLY_PRICE_LKR"]
    free_limit = app.config["FREE_RUN_LIMIT"]
    return [
        {
            "id": "free",
            "name": "Free trial",
            "price_lkr": 0,
            "days": 0,
            "featured": False,
            "tagline": f"{free_limit} matches after signup",
            "features": [
                f"{free_limit} CV matching runs",
                "Email CSV reports",
                "No card required to start",
            ],
            "cta_label": "Start free",
            "cta_href": "/register",
        },
        {
            "id": "monthly",
            "name": "1 month",
            "price_lkr": monthly,
            "days": app.config["PLAN_MONTHLY_DAYS"],
            "featured": False,
            "tagline": "Flexible month-to-month access",
            "features": [
                "Unlimited matching for 30 days",
                "Bank transfer or PayHere",
                "Email CSV reports",
            ],
            "cta_label": "Choose 1 month",
            "cta_href": "/subscribe?plan=monthly",
        },
        {
            "id": "yearly",
            "name": "1 year",
            "price_lkr": yearly,
            "days": app.config["PLAN_YEARLY_DAYS"],
            "featured": True,
            "tagline": "Best value for serious job seekers",
            "features": [
                "Unlimited matching for 365 days",
                f"Save vs paying monthly (LKR {monthly * 12 - yearly:,})",
                "Bank transfer or PayHere",
            ],
            "cta_label": "Choose 1 year",
            "cta_href": "/subscribe?plan=yearly",
        },
    ]


def _resolve_plan(app: Flask, plan_id: str | None) -> dict:
    wanted = (plan_id or "monthly").strip().lower()
    for plan in _plans(app):
        if plan["id"] == wanted and plan["id"] != "free":
            return plan
    return next(plan for plan in _plans(app) if plan["id"] == "monthly")


def _days_for_subscription(app: Flask, subscription) -> int:
    note = (getattr(subscription, "note", "") or "").lower()
    amount = int(getattr(subscription, "amount_lkr", 0) or 0)
    if "plan:yearly" in note or amount >= int(app.config["PLAN_YEARLY_PRICE_LKR"]):
        return int(app.config["PLAN_YEARLY_DAYS"])
    return int(app.config["PLAN_MONTHLY_DAYS"])


def _admin_users_page(auth_store: AuthStore, *, message: str | None = None, error: str | None = None):
    rows = []
    for item in auth_store.list_users():
        rows.append(
            {
                "user": item,
                "subscription": auth_store.active_subscription(item.id)
                or auth_store.latest_subscription(item.id),
            }
        )
    return render_template(
        "admin.html",
        users=rows,
        message=message,
        error=error,
    )


def _admin_feedback_page(
    auth_store: AuthStore, *, message: str | None = None, error: str | None = None
):
    return render_template(
        "admin_feedback.html",
        feedback=auth_store.list_feedback(),
        message=message,
        error=error,
    )


def _notify_subscription_email(app: Flask, auth_store: AuthStore, subscription) -> None:
    owner = auth_store.get_user(subscription.user_id)
    if owner is None:
        return
    plan = _resolve_plan(
        app,
        "yearly" if "plan:yearly" in (subscription.note or "").lower() else "monthly",
    )
    try:
        send_subscription_email(
            owner.email,
            plan_name=plan["name"],
            amount_lkr=int(subscription.amount_lkr),
            ends_at=subscription.ends_at,
        )
    except Exception:
        logger.exception(
            "Subscription email failed for %s",
            _masked_email(owner.email),
        )


def _contact_details(app: Flask) -> dict[str, str]:
    phone = app.config["CONTACT_PHONE"]
    whatsapp = app.config["CONTACT_WHATSAPP"] or "".join(ch for ch in phone if ch.isdigit())
    whatsapp_url = f"https://wa.me/{whatsapp}" if whatsapp else ""
    return {
        "email": app.config["CONTACT_EMAIL"],
        "phone": phone,
        "whatsapp_url": whatsapp_url,
        "hours": app.config["CONTACT_HOURS"],
    }


def _send_contact_email(destination: str, form: dict[str, str]) -> bool:
    if not _smtp_configured() or not destination:
        return False
    try:
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = f"[CareerSync] {form['subject']}"
        message["From"] = os.getenv("SMTP_FROM", destination).strip()
        message["To"] = destination
        message["Reply-To"] = form["email"]
        message.set_content(
            f"Name: {form['name']}\nEmail: {form['email']}\n\n{form['message']}\n"
        )
        host = os.getenv("SMTP_HOST", "").strip()
        port = int(os.getenv("SMTP_PORT", "587"))
        username = os.getenv("SMTP_USERNAME", "").strip()
        password = os.getenv("SMTP_PASSWORD", "").strip()
        use_tls = os.getenv("SMTP_USE_TLS", "1").strip().lower() in {"1", "true", "yes"}
        use_ssl = os.getenv("SMTP_USE_SSL", "0").strip().lower() in {"1", "true", "yes"}
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if use_tls:
                    smtp.starttls()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        return True
    except Exception:
        logger.exception("Failed to send contact email")
        return False


def _bank_details() -> dict[str, str]:
    return {
        "bank_name": os.getenv("BANK_NAME", "Commercial Bank of Ceylon").strip(),
        "account_name": os.getenv("BANK_ACCOUNT_NAME", "Kavithanjali Balakrishnan").strip(),
        "account_number": os.getenv("BANK_ACCOUNT_NUMBER", "8030603901").strip(),
        "branch": os.getenv("BANK_BRANCH", "Kotahena").strip(),
        "swift": os.getenv("BANK_SWIFT", "").strip(),
    }


def _payhere_enabled() -> bool:
    return bool(
        os.getenv("PAYHERE_MERCHANT_ID", "").strip()
        and os.getenv("PAYHERE_MERCHANT_SECRET", "").strip()
    )


def _payhere_form(
    email: str,
    order_id: str,
    *,
    amount_lkr: int,
    first_name: str = "Subscriber",
    last_name: str = "User",
    phone: str = "0700000000",
) -> dict[str, str]:
    merchant_id = os.getenv("PAYHERE_MERCHANT_ID", "").strip()
    merchant_secret = os.getenv("PAYHERE_MERCHANT_SECRET", "").strip()
    amount = f"{int(amount_lkr):.2f}"
    currency = "LKR"
    sandbox = os.getenv("PAYHERE_SANDBOX", "1").strip().lower() in {"1", "true", "yes"}
    checkout_url = (
        "https://sandbox.payhere.lk/pay/checkout"
        if sandbox
        else "https://www.payhere.lk/pay/checkout"
    )
    public_base = os.getenv("PUBLIC_BASE_URL", "https://careersync.best").rstrip("/")
    hashed_secret = hashlib.md5(merchant_secret.encode("utf-8")).hexdigest().upper()
    raw = f"{merchant_id}{order_id}{amount}{currency}{hashed_secret}"
    signature = hashlib.md5(raw.encode("utf-8")).hexdigest().upper()
    return {
        "merchant_id": merchant_id,
        "checkout_url": checkout_url,
        "return_url": f"{public_base}/payments/payhere/return",
        "cancel_url": f"{public_base}/subscribe",
        "notify_url": f"{public_base}/payments/payhere/notify",
        "order_id": order_id,
        "hash": signature,
        "amount": amount,
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
    }


def _ensure_bootstrap_admin(auth_store: AuthStore) -> None:
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not email or not password:
        return
    existing = auth_store.get_user_by_email(email)
    if existing is None:
        auth_store.create_user(email, password, is_admin=True)
        logger.info("Bootstrap admin created for %s", _masked_email(email))
        return
    if not existing.is_admin:
        # Recreate as admin if needed is intentional for first deploy only.
        logger.warning(
            "ADMIN_EMAIL exists but is not admin; activate payments via that account after promoting manually."
        )


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
