from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from .runner import RunSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_tls: bool = True
    use_ssl: bool = False

    @classmethod
    def from_environment(cls) -> "MailSettings":
        host = os.getenv("SMTP_HOST", "").strip()
        sender = os.getenv("SMTP_FROM", "").strip()
        if not host or not sender:
            raise RuntimeError("Email is not configured. Set SMTP_HOST and SMTP_FROM.")
        return cls(
            host=host,
            port=int(os.getenv("SMTP_PORT", "587")),
            username=os.getenv("SMTP_USERNAME", "").strip(),
            password=os.getenv("SMTP_PASSWORD", ""),
            sender=sender,
            use_tls=os.getenv("SMTP_USE_TLS", "1").lower() in {"1", "true", "yes"},
            use_ssl=os.getenv("SMTP_USE_SSL", "0").lower() in {"1", "true", "yes"},
        )


def build_results_message(recipient: str, summary: RunSummary) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"Your CV job matches - {summary.matches_written} matches"
    message["To"] = recipient
    message.set_content(
        "Your CV job search is complete.\n\n"
        f"Country: {summary.country}\n"
        f"Jobs discovered: {summary.jobs_fetched}\n"
        f"Related vacancies reviewed: {summary.related_jobs}\n"
        f"Matches: {summary.matches_written}\n"
        f"Manual-review vacancies: {summary.manual_review_jobs}\n\n"
        "The generated CSV reports are attached. Always verify eligibility and vacancy "
        "availability on the employer's official application page.\n"
    )
    for filename in (
        "all_discovered_jobs.csv",
        "related_vacancies.csv",
        "job_matches.csv",
        "rejected_vacancies.csv",
        "manual_review_vacancies.csv",
        "source_coverage.csv",
    ):
        path = summary.output_dir / filename
        if not path.is_file():
            continue
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (content_type or "text/csv").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return message


def send_results_email(
    recipient: str,
    summary: RunSummary,
    settings: MailSettings | None = None,
) -> None:
    settings = settings or MailSettings.from_environment()
    message = build_results_message(recipient, summary)
    message["From"] = settings.sender
    _deliver(message, settings)
    logger.info("Email delivery completed for %s", _masked_recipient(recipient))


def send_welcome_email(recipient: str, *, free_run_limit: int = 2) -> bool:
    base = _public_base()
    subject = "Welcome to CareerSync — you’re registered"
    text = (
        "Welcome to CareerSync!\n\n"
        "Your account was created successfully.\n"
        f"You have {free_run_limit} free matches to try the matcher.\n\n"
        f"Open the matcher: {base}/app\n"
        f"View plans: {base}/pricing\n"
    )
    html = _branded_html(
        eyebrow="Welcome",
        title="You’re registered successfully",
        body_html=(
            f"<p style='margin:0 0 14px;color:#5f6f66;line-height:1.6;'>"
            f"Thanks for joining <strong>CareerSync</strong>. Your account is ready.</p>"
            f"<p style='margin:0 0 14px;color:#5f6f66;line-height:1.6;'>"
            f"You get <strong>{free_run_limit} free matches</strong> to try live CV matching. "
            f"After that, choose a 1 month or 1 year plan.</p>"
        ),
        cta_label="Open matcher",
        cta_url=f"{base}/app",
        secondary_label="View plans",
        secondary_url=f"{base}/pricing",
    )
    return _send_html_email(recipient, subject, text, html)


def send_subscription_email(
    recipient: str,
    *,
    plan_name: str,
    amount_lkr: int,
    ends_at: str | None,
) -> bool:
    base = _public_base()
    subject = f"CareerSync subscription active — {plan_name}"
    ends_line = f"Access until: {ends_at}\n" if ends_at else ""
    text = (
        "Your CareerSync subscription is active.\n\n"
        f"Plan: {plan_name}\n"
        f"Amount: LKR {amount_lkr}\n"
        f"{ends_line}\n"
        f"Open the matcher: {base}/app\n"
    )
    ends_html = (
        f"<p style='margin:0 0 14px;color:#5f6f66;line-height:1.6;'>Access until "
        f"<strong>{ends_at}</strong>.</p>"
        if ends_at
        else ""
    )
    html = _branded_html(
        eyebrow="Subscription",
        title="Your plan is active",
        body_html=(
            f"<p style='margin:0 0 14px;color:#5f6f66;line-height:1.6;'>"
            f"Payment confirmed. Your <strong>{plan_name}</strong> plan "
            f"(LKR {amount_lkr:,}) is now active.</p>"
            f"{ends_html}"
            f"<p style='margin:0 0 14px;color:#5f6f66;line-height:1.6;'>"
            f"You can run unlimited CV matching during your subscription period.</p>"
        ),
        cta_label="Go to matcher",
        cta_url=f"{base}/app",
        secondary_label="Manage plans",
        secondary_url=f"{base}/pricing",
    )
    return _send_html_email(recipient, subject, text, html)


def _send_html_email(recipient: str, subject: str, text: str, html: str) -> bool:
    try:
        settings = MailSettings.from_environment()
    except RuntimeError:
        logger.warning("SMTP not configured; skipped email to %s", _masked_recipient(recipient))
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = recipient
    message["From"] = formataddr(("CareerSync", settings.sender))
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    try:
        _deliver(message, settings)
        logger.info("Transactional email sent to %s (%s)", _masked_recipient(recipient), subject)
        return True
    except Exception:
        logger.exception("Failed transactional email to %s", _masked_recipient(recipient))
        return False


def _deliver(message: EmailMessage, settings: MailSettings) -> None:
    smtp_class = smtplib.SMTP_SSL if settings.use_ssl else smtplib.SMTP
    logger.info("Connecting to SMTP server %s:%d", settings.host, settings.port)
    with smtp_class(settings.host, settings.port, timeout=30) as client:
        if settings.use_tls and not settings.use_ssl:
            client.starttls()
        if settings.username:
            client.login(settings.username, settings.password)
        client.send_message(message)


def _public_base() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://careersync.best").rstrip("/")


def _branded_html(
    *,
    eyebrow: str,
    title: str,
    body_html: str,
    cta_label: str,
    cta_url: str,
    secondary_label: str | None = None,
    secondary_url: str | None = None,
) -> str:
    base = _public_base()
    logo = f"{base}/static/logo.svg"
    secondary = ""
    if secondary_label and secondary_url:
        secondary = (
            f"<a href='{secondary_url}' style='display:inline-block;margin:0 0 0 10px;"
            f"padding:12px 18px;border-radius:12px;border:1.5px solid #cfdcd4;"
            f"color:#18352a;text-decoration:none;font-weight:700;'>{secondary_label}</a>"
        )
    return f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#eef3ee;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef3ee;padding:28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:18px;overflow:hidden;border:1px solid #d0ddd4;">
          <tr>
            <td style="background:#18352a;padding:22px 24px;">
              <img src="{logo}" alt="CareerSync" width="40" height="40" style="display:block;border-radius:10px;">
              <div style="margin-top:12px;color:#c8f23a;font-size:12px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">{eyebrow}</div>
              <div style="margin-top:6px;color:#ffffff;font-size:26px;font-weight:800;line-height:1.15;">CareerSync</div>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 24px 8px;">
              <h1 style="margin:0 0 14px;color:#14201a;font-size:24px;line-height:1.2;">{title}</h1>
              {body_html}
              <div style="margin:22px 0 8px;">
                <a href="{cta_url}" style="display:inline-block;padding:13px 18px;border-radius:12px;background:#c8f23a;color:#14201a;text-decoration:none;font-weight:800;">{cta_label}</a>
                {secondary}
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 24px 24px;color:#8a9a90;font-size:12px;line-height:1.5;">
              You’re receiving this because you use CareerSync at
              <a href="{base}" style="color:#2f8f6b;">{base.replace('https://', '')}</a>.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def _masked_recipient(recipient: str) -> str:
    local, separator, domain = recipient.partition("@")
    if not separator:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"
