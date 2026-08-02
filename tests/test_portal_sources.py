import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

import cv_job_matcher.job_sources as job_sources
from cv_job_matcher.job_sources import (
    Crawl4AiSeedProvider,
    RemoteRocketshipProvider,
    SRI_LANKA_PORTALS,
    SriLankaPortalProvider,
    default_providers,
    _html_links,
    _itpro_feed_urls,
    _jobish_link,
    _json_ld_items,
    _listing_like_url,
    _potential_job_detail_link,
    _portal_search_form_urls,
    _portal_title_matches,
    _position_variants,
    _target_role_matches,
)
from cv_job_matcher.models import CandidateProfile


def test_html_links_resolve_relative_job_urls():
    html = (
        '<a class="job" href="/jobs/devops-engineer">DevOps Engineer</a>'
        '<a href="/category/`https:/lankajob.lk/company/` + post.profile_slug">Broken</a>'
    )

    assert _html_links(html, "https://example.lk/jobs") == [
        ("DevOps Engineer", "https://example.lk/jobs/devops-engineer")
    ]


def test_portal_title_matching_uses_requested_field():
    profile = CandidateProfile("CV", target_position="DevOps Engineer")

    assert _portal_title_matches(profile, "Junior DevOps Engineer")
    assert not _portal_title_matches(profile, "Accounts Executive")


def test_search_page_is_not_treated_as_an_individual_job():
    assert _listing_like_url("https://www.hire.lk/jobs?q=DevOps+Engineer")
    assert not _listing_like_url("https://www.hire.lk/jobs/123/devops-engineer")


@pytest.mark.parametrize(
    ("url", "title"),
    [
        ("https://career141.com/it-jobs", "IT Jobs"),
        ("https://career141.com/pharmaceutical-jobs", "Pharmaceutical Jobs"),
        ("https://recruitme.lk/jobs-in-colombo", "Jobs in Colombo"),
        (
            "https://recruitme.lk/accounts-finance-management-jobs-in-colombo",
            "Accounts and finance jobs in Colombo",
        ),
        ("https://recruitme.lk/post/job/vacancy", "Post a job vacancy"),
        (
            "https://governmentjobs.lk/government_job_vacancies_in_sri_lanka.php",
            "Government job vacancies in Sri Lanka",
        ),
        (
            "https://www.hire.lk/auth/facebook/redirect?redirect_to=https%3A%2F%2Fwww.hire.lk%2Fjobs",
            "Continue with Facebook",
        ),
    ],
)
def test_portal_navigation_and_auth_links_are_not_detail_ads(url, title):
    assert not _potential_job_detail_link(url, title)


def test_malformed_social_share_url_is_rejected():
    html = (
        '<a href="/Home/showJobDetails/Accounting/20867/&quot;https:/twitter.com/share">'
        "Share job</a>"
    )

    assert _html_links(html, "https://www.timesjobs.lk/") == []


def test_jobish_link_does_not_treat_a_job_portal_hostname_as_job_evidence():
    assert not _jobish_link("https://careerlk.com/about-us", "About Us")
    assert _jobish_link("https://careerlk.com/job/accountant", "View details")


def test_every_pending_sri_lankan_board_has_one_generic_provider():
    expected = {
        "Jobber.lk",
        "JobFactory.lk",
        "DreamJobs.lk",
        "JobEka.lk",
        "FindMyJob.lk",
        "Career141",
        "TimesJobs.lk",
        "GovernmentJobs.lk",
        "GovernmentVacancies.lk",
        "Gazette.lk",
        "job.govdoc.lk",
        "SLBFE Job Bank",
        "LankaQualityJobs.com",
        "Recruitme.lk",
        "Jobup.lk",
        "MYJOBS.LK",
    }
    portal_names = [name for name, _ in SRI_LANKA_PORTALS]
    provider_names = [provider.name for provider in default_providers()]

    assert expected.issubset(portal_names)
    assert all(provider_names.count(name) == 1 for name in expected)
    assert sum("ikman" in name.lower() for name in portal_names) == 1


def test_devops_discovery_includes_related_role_family():
    assert _target_role_matches("Site Reliability Engineer", "DevOps Engineer")
    assert _target_role_matches("Junior Platform Engineer", "DevOps Engineer")
    assert not _target_role_matches("Accounts Executive", "DevOps Engineer")
    assert "cloud engineer" in _position_variants("DevOps Engineer")


def test_generic_role_discovery_uses_distinctive_position_tokens():
    assert _target_role_matches("Registered Nurse - Colombo", "Registered Nurse")
    assert _target_role_matches("Senior Financial Accountant", "Accountant")
    assert _target_role_matches("Digital Marketing Executive", "Marketing Executive")
    assert not _target_role_matches("Software Engineer", "Registered Nurse")


def test_itpro_feeds_are_discovered_from_position_with_all_jobs_fallback():
    html = """
    <a href="/rss/all/">Feed for all jobs</a>
    <a href="/rss/devops-cloud/">Feed for DevOps and Cloud</a>
    <a href="/rss/ai-and-data/">Feed for AI and Data</a>
    """

    feeds = _itpro_feed_urls(html, "DevOps Engineer")

    assert feeds == [
        "https://itpro.lk/rss/all/",
        "https://itpro.lk/rss/devops-cloud/",
    ]


def _job_posting_html(
    title: str,
    url: str,
    description: str = "Build reliable systems and apply now.",
) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "url": url,
        "description": description,
        "datePosted": "2026-07-28",
        "validThrough": "2099-12-31",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Colombo",
                "addressCountry": "LK",
            },
        },
    }
    return (
        "<html><head><script type=\"application/ld+json\">"
        f"{json.dumps(payload)}</script></head><body><h1>{title}</h1></body></html>"
    )


def test_json_ld_parser_supports_direct_graph_and_item_list_records():
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "WebSite", "name": "Portal"},
        {"@type": "JobPosting", "title": "DevOps Engineer", "url": "/jobs/1"}
      ]
    }
    </script>
    <script type="application/ld+json">
    {
      "@type": "ItemList",
      "itemListElement": [
        {"@type": "ListItem", "item": {
          "@type": "JobPosting", "title": "Accountant", "url": "/jobs/2"
        }}
      ]
    }
    </script>
    """

    jobs = [
        item
        for item in _json_ld_items(html)
        if item.get("@type") == "JobPosting"
    ]

    assert [item["title"] for item in jobs] == ["DevOps Engineer", "Accountant"]


def test_portal_search_forms_are_discovered_without_site_specific_code():
    html = """
    <form action="/find-jobs" method="get">
      <input type="hidden" name="country" value="lk">
      <input type="search" name="keywords" placeholder="Search jobs">
    </form>
    """

    assert _portal_search_form_urls(
        html,
        "https://example.lk/jobs",
        "Finance Manager",
    ) == ["https://example.lk/find-jobs?country=lk&keywords=Finance+Manager"]


def test_portal_provider_crawls_search_category_pagination_and_detail_pages(
    monkeypatch,
):
    seed = "https://example.lk/"
    search_url = "https://example.lk/find?keywords=DevOps+Engineer"
    pages = {
        seed: """
            <form action="/find"><input type="search" name="keywords"></form>
            <a href="/">DevOps Engineer</a>
            <a href="/job-category/technology">Technology jobs</a>
        """,
        search_url: """
            <a href="/find?keywords=DevOps+Engineer&page=2">Next</a>
            <a href="/opening/100">View job</a>
        """,
        "https://example.lk/job-category/technology": """
            <a href="/opening/200">View details</a>
        """,
        "https://example.lk/find?keywords=DevOps+Engineer&page=2": """
            <a href="/opening/300">View details</a>
        """,
        "https://example.lk/opening/100": _job_posting_html(
            "Accounts Executive",
            "https://example.lk/opening/100",
        ),
        "https://example.lk/opening/200": """
            <html><head><title>DevOps Engineer</title></head>
            <body><main><h1>DevOps Engineer</h1>
            <h2>Responsibilities</h2>
            Maintain cloud infrastructure and deployment pipelines.
            <h2>Requirements</h2>
            Linux, containers, and CI/CD experience. Apply now.
            </main></body></html>
        """,
        "https://example.lk/opening/300": _job_posting_html(
            "Platform Engineer",
            "https://example.lk/opening/300",
            "Own cloud platforms, Kubernetes, delivery pipelines, and reliability.",
        ),
    }
    requested: list[str] = []

    def fake_get_text(url, **_kwargs):
        requested.append(url)
        return pages[url]

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("Example Jobs", seed)

    jobs = provider.search(
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        10,
    )

    assert {job.title for job in jobs} == {"DevOps Engineer", "Platform Engineer"}
    assert all(job.url != seed for job in jobs)
    assert any("deployment pipelines" in job.description for job in jobs)
    assert search_url in requested
    assert "https://example.lk/job-category/technology" in requested
    assert "https://example.lk/find?keywords=DevOps+Engineer&page=2" in requested


@pytest.mark.parametrize(
    ("seed", "error"),
    [
        ("https://jobup.lk/", URLError("certificate verify failed")),
        ("https://www.careerfirst.lk/", TimeoutError("timed out")),
    ],
)
def test_portal_provider_returns_empty_when_a_seed_page_is_unreachable(
    monkeypatch,
    seed,
    error,
):
    def fake_get_text(url, **_kwargs):
        raise error

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("Example Jobs", seed)

    assert provider.search(
        CandidateProfile("CV", target_position="DevOps Engineer"),
        "Sri Lanka",
        5,
    ) == []


def test_portal_stops_after_a_full_batch_of_server_failures(monkeypatch, caplog):
    seed = "https://broken.example/jobs"
    detail_urls = [f"https://broken.example/vacancy/{index}" for index in range(12)]
    listing = "".join(
        f'<a href="{url}">AI Engineer {index}</a>'
        for index, url in enumerate(detail_urls)
    )
    requested_details = []

    def fake_get_text(url, **_kwargs):
        if url == seed:
            return listing
        requested_details.append(url)
        raise HTTPError(url, 500, "Internal Server Error", hdrs=None, fp=None)

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("Broken Jobs", seed)

    assert provider.search(
        CandidateProfile("CV", target_position="AI Engineer"),
        "Sri Lanka",
        10,
    ) == []
    assert len(requested_details) == provider.max_detail_workers
    assert "detail pages unavailable (6 attempted)" in caplog.text


def test_remote_rocketship_returns_empty_when_blocked(monkeypatch):
    def fake_get_text(url, **_kwargs):
        raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)

    jobs = RemoteRocketshipProvider().search(
        CandidateProfile("CV", target_position="AI Engineer"),
        "Sri Lanka",
        5,
    )

    assert jobs == []


def test_careerlk_accountant_category_is_prioritized_before_unrelated_categories(
    monkeypatch,
):
    seed = "https://careerlk.com/jobs/"
    query_url = "https://careerlk.com/jobs?search_keywords=Accountant"
    accounting_url = "https://careerlk.com/job-category/accounting-finance"
    job_url = "https://careerlk.com/job/accountant-frella-wellness-sri-lanka"
    pages = {
        query_url: """
            <a href="/job-category/technology">Information Technology</a>
            <a href="/job-category/sales-marketing">Sales &amp; Marketing</a>
            <a href="/job-category/accounting-finance">Accounting &amp; Finance</a>
        """,
        accounting_url: f'<a href="{job_url}">Accountant</a>',
        job_url: _job_posting_html("Accountant", job_url),
    }
    requested: list[str] = []

    def fake_get_text(url, **_kwargs):
        requested.append(url)
        return pages[url]

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("CareerLK", seed)
    provider.max_discovery_pages = 2
    provider.max_detail_pages = 4

    jobs = provider.search(
        CandidateProfile("CV", target_position="Accountant"),
        "Sri Lanka",
        1,
    )

    assert [job.url for job in jobs] == [job_url]
    assert requested[:2] == [query_url, accounting_url]
    assert "https://careerlk.com/job-category/technology" not in requested


def test_recruiter_healthcare_category_exposes_private_nurse(monkeypatch):
    seed = "https://www.recruiter.lk/jobs"
    query_url = "https://www.recruiter.lk/jobs?search=Registered+Nurse"
    healthcare_url = "https://www.recruiter.lk/jobs/healthcare-medical"
    job_url = "https://www.recruiter.lk/job/private-nurse"
    pages = {
        query_url: """
            <a href="/jobs/finance-accounting">Finance &amp; Accounting</a>
            <a href="/jobs/healthcare-medical">Healthcare &amp; Medical</a>
        """,
        healthcare_url: f'<a href="{job_url}">Private Nurse</a>',
        job_url: _job_posting_html("Private Nurse", job_url),
    }
    requested: list[str] = []

    def fake_get_text(url, **_kwargs):
        requested.append(url)
        return pages[url]

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("Recruiter.lk", seed)
    provider.max_discovery_pages = 2
    provider.max_detail_pages = 4

    jobs = provider.search(
        CandidateProfile("CV", target_position="Registered Nurse"),
        "Sri Lanka",
        1,
    )

    assert [job.url for job in jobs] == [job_url]
    assert requested[:2] == [query_url, healthcare_url]
    assert "https://www.recruiter.lk/jobs/finance-accounting" not in requested


def test_ikman_registered_nurse_uses_generic_modifier_stripped_search(
    monkeypatch,
):
    seed = "https://ikman.lk/en/ads/sri-lanka/jobs"
    exact_search = f"{seed}?query=Registered+Nurse"
    broad_search = f"{seed}?query=nurse"
    job_url = "https://ikman.lk/en/ad/nurse-maggona-kalutara"
    pages = {
        seed: """
            <form action="/en/ads/sri-lanka/jobs" method="get">
              <input type="search" name="query" placeholder="Search jobs">
            </form>
        """,
        exact_search: "<p>No exact matches</p>",
        broad_search: f'<a href="{job_url}">Nurse - Maggona</a>',
        job_url: _job_posting_html("Nurse - Maggona", job_url),
    }
    requested: list[str] = []

    def fake_get_text(url, **_kwargs):
        requested.append(url)
        return pages[url]

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("Ikman Jobs", seed)
    provider.max_discovery_pages = 3
    provider.max_detail_pages = 4

    jobs = provider.search(
        CandidateProfile("CV", target_position="Registered Nurse"),
        "Sri Lanka",
        1,
    )

    assert [job.url for job in jobs] == [job_url]
    assert broad_search in requested
    assert exact_search in requested


def test_portal_detail_fetches_are_concurrent_ordered_and_failure_isolated(
    monkeypatch,
):
    seed = "https://example.lk/jobs"
    detail_urls = [
        "https://example.lk/job/accountant-one",
        "https://example.lk/job/accountant-two",
        "https://example.lk/job/accountant-three",
    ]
    listing = "".join(
        f'<a href="{url}">Accountant {index}</a>'
        for index, url in enumerate(detail_urls, start=1)
    )
    delays = {
        detail_urls[0]: 0.06,
        detail_urls[1]: 0.01,
        detail_urls[2]: 0.02,
    }
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_get_text(url, **_kwargs):
        nonlocal active, max_active
        if url == seed:
            return listing
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(delays[url])
            if url == detail_urls[1]:
                raise OSError("detail unavailable")
            title = "Accountant One" if url == detail_urls[0] else "Accountant Three"
            return _job_posting_html(title, url)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(job_sources, "_get_text", fake_get_text)
    provider = SriLankaPortalProvider("Example Jobs", seed)
    provider.max_discovery_pages = 1
    provider.max_detail_pages = 3
    provider.max_detail_workers = 3

    jobs = provider.search(
        CandidateProfile("CV", target_position="Accountant"),
        "Sri Lanka",
        10,
    )

    assert max_active >= 2
    assert [job.url for job in jobs] == [detail_urls[0], detail_urls[2]]


def test_crawl4ai_maps_reversed_results_by_result_url(monkeypatch):
    seed = "https://example.lk/jobs"
    page_two = "https://example.lk/jobs?page=2"
    devops_url = "https://example.lk/opening/devops"
    accounts_url = "https://example.lk/opening/accounts"

    class FakeResult:
        def __init__(self, url, html="", links=None, markdown="", success=True):
            self.url = url
            self.html = html
            self.links = links or {"internal": []}
            self.markdown = markdown
            self.success = success
            self.error_message = ""

    results = {
        seed: FakeResult(
            seed,
            links={
                "internal": [
                    {"href": page_two, "text": "Next"},
                    {"href": accounts_url, "text": "View job"},
                ]
            },
        ),
        page_two: FakeResult(
            page_two,
            links={"internal": [{"href": devops_url, "text": "View details"}]},
        ),
        devops_url: FakeResult(
            devops_url,
            html=_job_posting_html("DevOps Engineer", devops_url),
        ),
        accounts_url: FakeResult(
            accounts_url,
            html=_job_posting_html("Accounts Executive", accounts_url),
        ),
    }

    class FakeCrawler:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def arun_many(self, urls, **_kwargs):
            return [results[url] for url in reversed(urls)]

    fake_module = SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CrawlerRunConfig=lambda **kwargs: kwargs,
    )
    monkeypatch.setitem(sys.modules, "crawl4ai", fake_module)

    jobs = asyncio.run(
        Crawl4AiSeedProvider()._crawl(
            [seed],
            CandidateProfile("CV", target_position="DevOps Engineer"),
            "Sri Lanka",
            5,
        )
    )

    assert [(job.title, job.url) for job in jobs] == [
        ("DevOps Engineer", devops_url)
    ]
