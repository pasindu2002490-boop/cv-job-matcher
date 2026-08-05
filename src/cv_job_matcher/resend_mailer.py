from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .runner import RunSummary


class EmailDeliveryError(RuntimeError):
    """The results email was not accepted by the HTTPS email provider."""


@dataclass(frozen=True)
class ResendSettings:
    api_key: str
    sender: str
    reply_to: str = ""
    endpoint: str = "https://api.resend.com/emails"


_RESULT_ATTACHMENTS = (
    "all_discovered_jobs.csv",
    "related_vacancies.csv",
    "job_matches.csv",
)


def send_results_via_resend(
    recipient: str,
    summary: RunSummary,
    *,
    task_id: str,
    settings: ResendSettings,
    attachment_names: Iterable[str] = _RESULT_ATTACHMENTS,
    timeout_seconds: float = 30,
) -> str:
    if not settings.api_key.strip() or not settings.sender.strip():
        raise EmailDeliveryError("RESEND_API_KEY and RESEND_FROM are required")
    attachments = []
    for filename in attachment_names:
        path = summary.output_dir / Path(filename).name
        if path.is_file():
            attachments.append(
                {
                    "filename": path.name,
                    "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
    body = {
        "from": settings.sender,
        "to": [recipient],
        "subject": f"Your CV job matches - {summary.matches_written} matches",
        "text": (
            "Your CV job search is complete.\n\n"
            f"Country: {summary.country}\n"
            f"Jobs in the task snapshot: {summary.jobs_fetched}\n"
            f"Related vacancies reviewed: {summary.related_jobs}\n"
            f"Matches: {summary.matches_written}\n"
            f"Manual-review vacancies: {summary.manual_review_jobs}\n\n"
            "Always verify eligibility and availability on the employer's page."
        ),
        "attachments": attachments,
    }
    if settings.reply_to.strip():
        body["reply_to"] = settings.reply_to.strip()
    request = Request(
        settings.endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": f"task-{task_id}-results-v1",
            "User-Agent": "cv-job-matcher-cloud/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise EmailDeliveryError(f"Resend returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise EmailDeliveryError("Resend is temporarily unavailable") from exc
    message_id = str(payload.get("id") or "").strip()
    if not message_id:
        raise EmailDeliveryError("Resend accepted no message identifier")
    return message_id
