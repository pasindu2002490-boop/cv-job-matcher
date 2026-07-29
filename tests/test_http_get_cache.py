import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError

import cv_job_matcher.job_sources as sources
import pytest


class FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.body = body.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


@pytest.fixture(autouse=True)
def isolated_http_get_cache(monkeypatch):
    monkeypatch.setenv("HTTP_GET_CACHE_MINUTES", "5")
    monkeypatch.setenv("HTTP_GET_CACHE_MAX_ENTRIES", "512")
    monkeypatch.setenv("HTTP_GET_CACHE_MAX_BYTES", str(32 * 1024 * 1024))
    sources._clear_http_get_cache()
    yield
    sources._clear_http_get_cache()


def test_get_text_reuses_successful_response(monkeypatch):
    calls = 0

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        assert timeout == 30
        return FakeResponse("same immutable text")

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)

    first = sources._get_text("https://example.test/jobs")
    second = sources._get_text("https://example.test/jobs")

    assert first == second == "same immutable text"
    assert isinstance(second, str)
    assert calls == 1


def test_settings_are_hard_bounded_and_non_finite_ttl_is_rejected(monkeypatch):
    monkeypatch.setenv("HTTP_GET_CACHE_MINUTES", "inf")
    monkeypatch.setenv("HTTP_GET_CACHE_MAX_ENTRIES", "999999999")
    monkeypatch.setenv("HTTP_GET_CACHE_MAX_BYTES", "999999999999")

    ttl_seconds, max_entries, max_bytes = sources._http_get_cache_settings()

    assert ttl_seconds == 300
    assert max_entries == sources._HTTP_GET_CACHE_HARD_MAX_ENTRIES
    assert max_bytes == sources._HTTP_GET_CACHE_HARD_MAX_BYTES

    monkeypatch.setenv("HTTP_GET_CACHE_MINUTES", "999999")
    ttl_seconds, _, _ = sources._http_get_cache_settings()
    assert ttl_seconds == sources._HTTP_GET_CACHE_MAX_TTL_SECONDS


def test_cache_metadata_hashes_secret_request_values():
    secret = "secret-value-that-must-not-be-retained-in-key"
    key = sources._http_get_cache_key(
        f"https://example.test/jobs?api_key={secret}",
        {"Authorization": f"Bearer {secret}"},
        "utf-8",
        30,
    )

    assert len(key) == 64
    assert secret not in key


def test_cache_key_includes_headers_encoding_and_timeout(monkeypatch):
    calls = 0

    def fake_urlopen(_request, _timeout=None, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(f"response-{calls}")

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)
    url = "https://example.test/jobs"

    first = sources._get_text(url, headers={"X-Tenant": "a"}, timeout=5)
    same_semantics = sources._get_text(url, headers={"x-tenant": "a"}, timeout=5)
    other_header = sources._get_text(url, headers={"X-Tenant": "b"}, timeout=5)
    other_encoding = sources._get_text(url, headers={"X-Tenant": "a"}, encoding="latin-1", timeout=5)
    other_timeout = sources._get_text(url, headers={"X-Tenant": "a"}, timeout=6)

    assert first == same_semantics
    assert len({first, other_header, other_encoding, other_timeout}) == 4
    assert calls == 4


def test_errors_and_unsuccessful_responses_are_not_cached(monkeypatch):
    calls = 0

    def flaky_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise URLError("temporary failure")
        return FakeResponse("service unavailable" if calls == 2 else "recovered", status=503 if calls == 2 else 200)

    monkeypatch.setattr(sources, "urlopen", flaky_urlopen)
    url = "https://example.test/jobs"

    with pytest.raises(URLError):
        sources._get_text(url)
    assert sources._get_text(url) == "service unavailable"
    assert sources._get_text(url) == "recovered"
    assert sources._get_text(url) == "recovered"
    assert calls == 3


def test_invalid_structured_payload_is_evicted(monkeypatch):
    calls = 0

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse("not-json" if calls == 1 else '{"jobs": []}')

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)

    with pytest.raises(ValueError):
        sources._get_json("https://example.test/api")
    assert sources._get_json("https://example.test/api") == {"jobs": []}
    assert calls == 2


def test_ttl_expiry_refreshes_response(monkeypatch):
    calls = 0
    now = [100.0]

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse(f"response-{calls}")

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)
    monkeypatch.setattr(sources.time, "monotonic", lambda: now[0])
    monkeypatch.setenv("HTTP_GET_CACHE_MINUTES", "0.1")
    url = "https://example.test/jobs"

    assert sources._get_text(url) == "response-1"
    now[0] += 5.9
    assert sources._get_text(url) == "response-1"
    now[0] += 0.2
    assert sources._get_text(url) == "response-2"
    assert calls == 2


def test_lru_is_bounded_and_recent_hit_is_retained(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(request.full_url)

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)
    monkeypatch.setenv("HTTP_GET_CACHE_MAX_ENTRIES", "2")
    first = "https://example.test/first"
    second = "https://example.test/second"
    third = "https://example.test/third"

    sources._get_text(first)
    sources._get_text(second)
    sources._get_text(first)
    sources._get_text(third)
    sources._get_text(first)
    sources._get_text(second)

    assert calls == [first, second, third, second]


def test_zero_ttl_disables_cache(monkeypatch):
    calls = 0

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse("text")

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)
    monkeypatch.setenv("HTTP_GET_CACHE_MINUTES", "0")

    sources._get_text("https://example.test/jobs")
    sources._get_text("https://example.test/jobs")

    assert calls == 2


def test_test_reset_refuses_to_clear_an_active_flight():
    flight = sources._HttpGetFlight()
    with sources._HTTP_GET_CACHE_LOCK:
        sources._HTTP_GET_INFLIGHT["test-flight"] = flight
    try:
        with pytest.raises(RuntimeError, match="in flight"):
            sources._clear_http_get_cache()
    finally:
        flight.event.set()
        sources._clear_http_get_cache()


def test_concurrent_identical_get_is_single_flight(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()
    request_started = threading.Event()
    release_response = threading.Event()

    def fake_urlopen(_request, timeout):
        nonlocal calls
        with calls_lock:
            calls += 1
        request_started.set()
        assert release_response.wait(timeout=2)
        return FakeResponse("one shared download")

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)
    url = "https://example.test/jobs"
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(sources._get_text, url) for _ in range(8)]
        assert request_started.wait(timeout=1)
        release_response.set()
        results = [future.result(timeout=2) for future in futures]

    assert results == ["one shared download"] * 8
    assert calls == 1


def test_single_flight_error_is_shared_but_not_cached(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()
    request_started = threading.Event()
    release_error = threading.Event()
    callers_ready = threading.Barrier(7)

    def fake_urlopen(_request, timeout):
        nonlocal calls
        with calls_lock:
            calls += 1
            current_call = calls
        if current_call == 1:
            request_started.set()
            assert release_error.wait(timeout=2)
            raise URLError("shared temporary failure")
        return FakeResponse("retry succeeded")

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)
    url = "https://example.test/jobs"

    def simultaneous_get():
        callers_ready.wait(timeout=2)
        return sources._get_text(url)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(simultaneous_get) for _ in range(6)]
        callers_ready.wait(timeout=2)
        assert request_started.wait(timeout=1)
        time.sleep(0.02)
        release_error.set()
        for future in futures:
            with pytest.raises(URLError, match="shared temporary failure"):
                future.result(timeout=2)

    assert calls == 1
    assert sources._get_text(url) == "retry succeeded"
    assert calls == 2
