from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class JobLaunchError(RuntimeError):
    """Cloud Run accepted neither the queued task nor a recovery execution."""


@dataclass(frozen=True)
class LaunchReceipt:
    operation_name: str


class MatchingJobLauncher(Protocol):
    def launch(self) -> LaunchReceipt: ...


class NoopMatchingJobLauncher:
    """Useful when a scheduler or local worker already drains the queue."""

    def launch(self) -> LaunchReceipt:
        return LaunchReceipt(operation_name="queue-only")


class CloudRunMatchingJobLauncher:
    """Launch a generic worker execution; the worker claims its task in PostgreSQL."""

    def __init__(
        self,
        project: str,
        region: str,
        job_name: str,
        *,
        timeout_seconds: float = 20,
    ) -> None:
        self._project = project.strip()
        self._region = region.strip()
        self._job_name = job_name.strip()
        self._timeout_seconds = timeout_seconds
        if not all((self._project, self._region, self._job_name)):
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT, CLOUD_RUN_REGION and MATCHER_JOB_NAME are required"
            )

    def launch(self) -> LaunchReceipt:
        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest
        except ImportError as exc:
            raise JobLaunchError(
                "Install the cloud dependency group to launch Cloud Run Jobs"
            ) from exc

        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(GoogleAuthRequest())
            access_token = credentials.token
        except Exception as exc:
            raise JobLaunchError("Could not obtain a Cloud Run service token") from exc
        if not access_token:
            raise JobLaunchError("The Cloud Run service identity returned no access token")

        path = (
            f"projects/{quote(self._project, safe='')}/locations/"
            f"{quote(self._region, safe='')}/jobs/{quote(self._job_name, safe='')}:run"
        )
        request = Request(
            f"https://run.googleapis.com/v2/{path}",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "cv-job-matcher-cloud/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(
                    response.read().decode("utf-8", errors="replace")
                )
        except HTTPError as exc:
            raise JobLaunchError(f"Cloud Run Jobs returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise JobLaunchError("Cloud Run Jobs could not be launched") from exc
        return LaunchReceipt(operation_name=str(payload.get("name") or "accepted"))

