from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


class ObjectStorageError(RuntimeError):
    """A private object could not be stored or retrieved."""


class PrivateObjectStore(Protocol):
    provider_name: str

    def upload(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        overwrite: bool = False,
    ) -> None: ...

    def download(self, bucket: str, object_key: str) -> bytes: ...

    def delete(self, bucket: str, object_key: str) -> None: ...

    def create_signed_url(
        self,
        bucket: str,
        object_key: str,
        *,
        expires_seconds: int,
    ) -> str: ...


def _safe_bucket(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value):
        raise ValueError("Invalid storage bucket")
    return value


def _safe_key(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if not normalized or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid storage object key")
    return "/".join(parts)


def _quoted_path(bucket: str, object_key: str) -> str:
    parts = [_safe_bucket(bucket), *_safe_key(object_key).split("/")]
    return "/".join(quote(part, safe="") for part in parts)


class LocalPrivateObjectStore:
    """Development adapter with the same private-object contract as Supabase."""

    provider_name = "local"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, bucket: str, object_key: str) -> Path:
        target = (self._root / _safe_bucket(bucket) / _safe_key(object_key)).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("Storage path escaped its configured root") from exc
        return target

    def upload(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        overwrite: bool = False,
    ) -> None:
        del content_type
        path = self._path(bucket, object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise ObjectStorageError("The object already exists")
        path.write_bytes(content)

    def download(self, bucket: str, object_key: str) -> bytes:
        try:
            return self._path(bucket, object_key).read_bytes()
        except OSError as exc:
            raise ObjectStorageError("The object could not be read") from exc

    def delete(self, bucket: str, object_key: str) -> None:
        path = self._path(bucket, object_key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ObjectStorageError("The object could not be deleted") from exc

    def create_signed_url(
        self,
        bucket: str,
        object_key: str,
        *,
        expires_seconds: int,
    ) -> str:
        del expires_seconds
        return self._path(bucket, object_key).as_uri()


class SupabasePrivateObjectStore:
    """Small HTTPS adapter for private Supabase Storage buckets."""

    provider_name = "supabase"

    def __init__(
        self,
        supabase_url: str,
        service_role_key: str,
        *,
        timeout_seconds: float = 30,
    ) -> None:
        self._base_url = supabase_url.rstrip("/")
        self._service_role_key = service_role_key.strip()
        self._timeout_seconds = timeout_seconds
        if not self._base_url or not self._service_role_key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for storage"
            )

    def _headers(self, **extra: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._service_role_key}",
            "apikey": self._service_role_key,
            "User-Agent": "cv-job-matcher-cloud/0.1",
            **extra,
        }

    def _open(self, request: Request) -> bytes:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            raise ObjectStorageError(
                f"Supabase Storage returned HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ObjectStorageError("Supabase Storage is unavailable") from exc

    def upload(
        self,
        bucket: str,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        overwrite: bool = False,
    ) -> None:
        path = _quoted_path(bucket, object_key)
        request = Request(
            f"{self._base_url}/storage/v1/object/{path}",
            data=content,
            headers=self._headers(
                **{
                    "Content-Type": content_type or "application/octet-stream",
                    "x-upsert": "true" if overwrite else "false",
                }
            ),
            method="POST",
        )
        self._open(request)

    def download(self, bucket: str, object_key: str) -> bytes:
        path = _quoted_path(bucket, object_key)
        request = Request(
            f"{self._base_url}/storage/v1/object/authenticated/{path}",
            headers=self._headers(Accept="application/octet-stream"),
            method="GET",
        )
        return self._open(request)

    def delete(self, bucket: str, object_key: str) -> None:
        bucket_name = _safe_bucket(bucket)
        key = _safe_key(object_key)
        request = Request(
            f"{self._base_url}/storage/v1/object/{quote(bucket_name, safe='')}",
            data=json.dumps({"prefixes": [key]}).encode("utf-8"),
            headers=self._headers(**{"Content-Type": "application/json"}),
            method="DELETE",
        )
        self._open(request)

    def create_signed_url(
        self,
        bucket: str,
        object_key: str,
        *,
        expires_seconds: int,
    ) -> str:
        if not 1 <= expires_seconds <= 604_800:
            raise ValueError("Signed URL lifetime must be between 1 second and 7 days")
        path = _quoted_path(bucket, object_key)
        request = Request(
            f"{self._base_url}/storage/v1/object/sign/{path}",
            data=json.dumps({"expiresIn": expires_seconds}).encode("utf-8"),
            headers=self._headers(**{"Content-Type": "application/json"}),
            method="POST",
        )
        try:
            payload = json.loads(self._open(request).decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ObjectStorageError("Supabase returned an invalid signed URL") from exc
        signed = str(payload.get("signedURL") or payload.get("signedUrl") or "").strip()
        if not signed:
            raise ObjectStorageError("Supabase did not return a signed URL")
        return signed if signed.startswith(("http://", "https://")) else urljoin(
            f"{self._base_url}/storage/v1/", signed.lstrip("/")
        )

