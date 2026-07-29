import json
import threading
import time
from dataclasses import replace
from urllib.error import HTTPError, URLError

import pytest

import cv_job_matcher.job_sources as sources
from cv_job_matcher.job_sources import (
    DuckDuckGoDiscoveryProvider,
    GoogleCustomSearchProvider,
    SerpApiGoogleProvider,
    _portal_job_from_page,
    _search_result_job,
    _validate_search_discovery_candidates,
)
from cv_job_matcher.matcher import filter_fresh_jobs
from cv_job_matcher.models import CandidateProfile, Job


def _posting_html(
    url: str,
    *,
    title: str = "DevOps Engineer",
    location: str = "Colombo",
    country: str = "Sri Lanka",
    date_posted: str = "",
    valid_through: str = "2099-12-31",
) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "url": url,
        "title": title,
        "description": (
            "Build cloud infrastructure. Responsibilities include deployments, "
            "monitoring, and incident response. Apply now."
        ),
        "hiringOrganization": {
            "@type": "Organization",
            "name": "Example Company",
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": location,
                "addressCountry": country,
            },
        },
        "validThrough": valid_through,
    }
    if date_posted:
        posting["datePosted"] = date_posted
    return (
        '<html><script type="application/ld+json">'
        f"{json.dumps(posting)}</script><body><h1>{title}</h1></body></html>"
    )


def _candidate(source: str, url: str, title: str = "DevOps Engineer") -> Job:
    return _search_result_job(
        source,
        title,
        url,
        f"{title} vacancy in Sri Lanka. Apply now.",
        "sri lanka",
    )


def test_duckduckgo_returns_only_fetched_live_role_detail_pages(monkeypatch):
    good_url = "https://jobs.example.com/vacancy/devops"
    expired_url = "https://jobs.example.com/vacancy/expired-devops"
    unrelated_url = "https://jobs.example.com/vacancy/accountant"
    html = "".join(
        (
            f'<a class="result__a" href="{url}">{title} job</a>'
            f'<div class="result__snippet">{title} vacancy in Sri Lanka. Apply now.</div>'
        )
        for url, title in (
            (good_url, "DevOps Engineer"),
            (expired_url, "DevOps Engineer"),
            (unrelated_url, "DevOps Engineer"),
        )
    )
    pages = {
        good_url: _posting_html(good_url),
        expired_url: _posting_html(expired_url, valid_through="2020-01-01"),
        unrelated_url: _posting_html(unrelated_url, title="Accountant"),
    }
    monkeypatch.setattr(sources, "_post_text", lambda *_args, **_kwargs: html)
    monkeypatch.setattr(
        sources,
        "_get_text",
        lambda url, **_kwargs: pages[url],
    )
    monkeypatch.setenv("WEB_DISCOVERY_MAX_QUERIES_PER_SOURCE", "1")

    jobs = DuckDuckGoDiscoveryProvider().search(
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        10,
    )

    assert [job.url for job in jobs] == [good_url]
    assert jobs[0].source == "DuckDuckGo Discovery"
    assert jobs[0].company == "Example Company"
    assert jobs[0].location == "Colombo, Sri Lanka"
    assert jobs[0].published_at == ""
    assert jobs[0].detail_page_verified
    assert "Build cloud infrastructure" in jobs[0].description


@pytest.mark.parametrize(
    ("provider", "payload_key", "source_name"),
    [
        (GoogleCustomSearchProvider(), "items", "Google Custom Search"),
        (SerpApiGoogleProvider(), "organic_results", "SerpAPI Google"),
    ],
)
def test_paid_search_adapters_bound_queries_and_validate_details(
    monkeypatch,
    provider,
    payload_key,
    source_name,
):
    url = "https://careers.example.com/opening/devops"
    calls = []

    def fake_get_json(_endpoint, params):
        calls.append(params["q"])
        return {
            payload_key: [
                {
                    "title": "DevOps Engineer job",
                    "link": url,
                    "snippet": "DevOps Engineer vacancy in Sri Lanka. Apply now.",
                }
            ]
        }

    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "test-cx")
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    monkeypatch.setenv("WEB_DISCOVERY_MAX_QUERIES_PER_SOURCE", "2")
    monkeypatch.setattr(sources, "_get_json", fake_get_json)
    monkeypatch.setattr(
        sources,
        "_get_text",
        lambda requested_url, **_kwargs: _posting_html(requested_url),
    )

    jobs = provider.search(
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        1,
    )

    assert len(calls) == 2
    assert len(jobs) == 1
    assert jobs[0].source == source_name
    assert jobs[0].detail_page_verified


def test_search_api_keeps_prior_candidates_if_a_later_query_fails(monkeypatch):
    url = "https://careers.example.com/opening/devops"
    calls = 0

    def fake_get_json(_endpoint, _params):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "items": [
                    {
                        "title": "DevOps Engineer job",
                        "link": url,
                        "snippet": "DevOps vacancy in Sri Lanka. Apply now.",
                    }
                ]
            }
        raise URLError("quota endpoint unavailable")

    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "test-cx")
    monkeypatch.setenv("WEB_DISCOVERY_MAX_QUERIES_PER_SOURCE", "3")
    monkeypatch.setattr(sources, "_get_json", fake_get_json)
    monkeypatch.setattr(
        sources,
        "_get_text",
        lambda requested_url, **_kwargs: _posting_html(requested_url),
    )

    jobs = GoogleCustomSearchProvider().search(
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        1,
    )

    assert calls == 2
    assert [job.url for job in jobs] == [url]


def test_google_custom_search_returns_empty_when_authorization_fails(monkeypatch):
    def fake_get_json(_endpoint, _params):
        raise HTTPError("https://www.googleapis.com/customsearch/v1", 401, "Unauthorized", None, None)

    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "test-cx")
    monkeypatch.setattr(sources, "_get_json", fake_get_json)

    jobs = GoogleCustomSearchProvider().search(
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        5,
    )

    assert jobs == []


def test_search_detail_validation_is_bounded_concurrent_and_ordered(monkeypatch):
    candidates = [
        _candidate("DuckDuckGo Discovery", f"https://example.com/opening/{index}")
        for index in range(8)
    ]
    requested = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_get_text(url, **_kwargs):
        nonlocal active, max_active
        with lock:
            requested.append(url)
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            return _posting_html(url)
        finally:
            with lock:
                active -= 1

    monkeypatch.setenv("WEB_DISCOVERY_MAX_DETAIL_PAGES_PER_SOURCE", "3")
    monkeypatch.setenv("WEB_DISCOVERY_DETAIL_WORKERS", "3")
    monkeypatch.setattr(sources, "_get_text", fake_get_text)

    jobs = _validate_search_discovery_candidates(
        candidates,
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        50,
    )

    assert len(requested) == 3
    assert max_active >= 2
    assert [job.url for job in jobs] == [
        "https://example.com/opening/0",
        "https://example.com/opening/1",
        "https://example.com/opening/2",
    ]


def test_distinct_detail_ids_in_query_strings_are_not_collapsed(monkeypatch):
    urls = [
        "https://jobs.example.lk/view?job_id=100",
        "https://jobs.example.lk/view?job_id=200",
    ]
    monkeypatch.setattr(
        sources,
        "_get_text",
        lambda url, **_kwargs: _posting_html(url),
    )

    jobs = _validate_search_discovery_candidates(
        [_candidate("Google Custom Search", url) for url in urls],
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        10,
    )

    assert [job.url for job in jobs] == urls
    assert len({job.source_id for job in jobs}) == 2


def test_unstructured_detail_retains_page_location_and_date(monkeypatch):
    url = "https://example.com/opening/devops"
    html = """
    <html>
      <head>
        <meta name="jobLocation" content="Colombo, Sri Lanka">
        <meta property="article:published_time" content="2026-07-28T09:00:00+05:30">
        <title>DevOps Engineer</title>
      </head>
      <body>
        <main>
          <h1>DevOps Engineer</h1>
          <h2>Responsibilities</h2>
          Maintain Kubernetes infrastructure and delivery pipelines.
          <h2>Requirements</h2>
          Linux and cloud experience. Apply now.
        </main>
      </body>
    </html>
    """
    monkeypatch.setattr(sources, "_get_text", lambda *_args, **_kwargs: html)

    jobs = _validate_search_discovery_candidates(
        [_candidate("Google Custom Search", url)],
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        1,
    )

    assert len(jobs) == 1
    assert jobs[0].location == "Colombo, Sri Lanka"
    assert jobs[0].published_at == "2026-07-28T09:00:00+05:30"
    assert jobs[0].detail_page_verified


def test_search_detail_without_target_country_or_with_closed_notice_is_rejected(
    monkeypatch,
):
    foreign_url = "https://example.com/opening/foreign"
    closed_url = "https://example.com/opening/closed"
    pages = {
        foreign_url: _posting_html(
            foreign_url,
            location="Berlin",
            country="Germany",
        ),
        closed_url: (
            _posting_html(closed_url)
            + "<p>This job is no longer available.</p>"
        ),
    }
    monkeypatch.setattr(sources, "_get_text", lambda url, **_kwargs: pages[url])

    jobs = _validate_search_discovery_candidates(
        [
            _candidate("SerpAPI Google", foreign_url),
            _candidate("SerpAPI Google", closed_url),
        ],
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        10,
    )

    assert jobs == []


def test_inactive_message_in_javascript_template_does_not_close_live_job(monkeypatch):
    url = "https://example.com/opening/devops"
    html = (
        _posting_html(url)
        + '<script>const inactiveMessage = "job is no longer available";</script>'
    )
    monkeypatch.setattr(sources, "_get_text", lambda *_args, **_kwargs: html)

    jobs = _validate_search_discovery_candidates(
        [_candidate("DuckDuckGo Discovery", url)],
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        1,
    )

    assert [job.url for job in jobs] == [url]


def test_freshness_accepts_undated_verified_detail_but_not_raw_snippet():
    raw = _candidate(
        "Google Custom Search",
        "https://example.com/opening/devops",
    )
    verified = replace(
        raw,
        source_id="verified",
        detail_page_verified=True,
        description="Fetched job detail with responsibilities and requirements.",
    )

    kept, rejected = filter_fresh_jobs([raw, verified], max_age_days=30)

    assert kept == [verified]
    assert rejected == 1


def test_freshness_accepts_undated_validated_crawl4ai_detail_only():
    url = "https://example.lk/opening/verified"
    validated = _portal_job_from_page(
        source="Crawl4AI Seeds",
        url=url,
        html=_posting_html(url),
        fallback_title="DevOps Engineer",
        profile=CandidateProfile("CV", target_position="DevOps Engineer"),
        country="Sri Lanka",
    )
    assert validated is not None
    assert validated.published_at == ""
    assert validated.detail_page_verified
    raw = replace(
        validated,
        source_id="raw",
        url="https://example.lk/opening/raw",
        detail_page_verified=False,
    )

    kept, rejected = filter_fresh_jobs([raw, validated], max_age_days=30)

    assert kept == [validated]
    assert rejected == 1
