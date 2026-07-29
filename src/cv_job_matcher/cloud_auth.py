from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AuthenticationError(RuntimeError):
    """A request did not contain a currently valid user identity."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str = ""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class SupabaseTokenVerifier:
    """Validate access tokens against Supabase Auth instead of trusting JWT claims."""

    def __init__(
        self,
        supabase_url: str,
        publishable_key: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        self._base_url = supabase_url.rstrip("/")
        self._publishable_key = publishable_key.strip()
        self._timeout_seconds = timeout_seconds
        if not self._base_url or not self._publishable_key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are required for authentication"
            )

    def verify(self, token: str) -> Principal:
        request = Request(
            f"{self._base_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": self._publishable_key,
                "Accept": "application/json",
                "User-Agent": "cv-job-matcher-cloud/0.1",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(
                    response.read().decode("utf-8", errors="replace")
                )
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise AuthenticationError("The access token is invalid or expired") from exc
            raise AuthenticationError("The authentication service rejected the request") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise AuthenticationError(
                "The authentication service is temporarily unavailable"
            ) from exc
        if not isinstance(payload, dict) or not str(payload.get("id") or "").strip():
            raise AuthenticationError("The authentication response did not contain a user")
        return Principal(
            user_id=str(payload["id"]).strip(),
            email=str(payload.get("email") or "").strip(),
        )


def bearer_token(headers: Mapping[str, str]) -> str:
    raw = str(headers.get("Authorization") or "").strip()
    scheme, separator, token = raw.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("A Bearer access token is required")
    return token.strip()

