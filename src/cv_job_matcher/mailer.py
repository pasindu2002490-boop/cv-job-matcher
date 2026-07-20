from __future__ import annotations

import mimetypes
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from .runner import RunSummary


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
    message["Subject"] = f"Your CV job matches — {summary.matches_written} matches"
    message["To"] = recipient
    message.set_content(
        "Your CV job search is complete.\n\n"
        f"Country: {summary.country}\n"
        f"Jobs discovered: {summary.jobs_fetched}\n"
        f"Matches: {summary.matches_written}\n\n"
        "The generated CSV reports are attached. Always verify eligibility and vacancy "
        "availability on the employer's official application page.\n"
    )
    for path in sorted(summary.output_dir.glob("*.csv")):
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (content_type or "text/csv").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return message


def send_results_email(recipient: str, summary: RunSummary, settings: MailSettings | None = None) -> None:
    settings = settings or MailSettings.from_environment()
    message = build_results_message(recipient, summary)
    message["From"] = settings.sender

    smtp_class = smtplib.SMTP_SSL if settings.use_ssl else smtplib.SMTP
    with smtp_class(settings.host, settings.port, timeout=30) as client:
        if settings.use_tls and not settings.use_ssl:
            client.starttls()
        if settings.username:
            client.login(settings.username, settings.password)
        client.send_message(message)
