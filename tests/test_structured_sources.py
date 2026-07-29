from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone

import cv_job_matcher.job_sources as sources
import pytest
from cv_job_matcher.job_sources import (
    ITProSriLankaProvider,
    TopJobsSriLankaProvider,
    XpressJobsSriLankaProvider,
    _cached_inventory,
    _enrich_itpro_jobs,
    _enrich_topjobs_jobs,
    _enrich_xpress_jobs,
    _itpro_category_urls,
    _position_variants,
    _sort_structured_jobs,
    _target_role_matches,
    _topjobs_inventory_roots,
    _xpress_expiry_is_past,
    _xpress_is_foreign_location,
)
from cv_job_matcher.models import CandidateProfile, Job


@pytest.fixture(autouse=True)
def clear_structured_source_caches():
    sources._INVENTORY_CACHE.clear()
    sources._ITPRO_DETAIL_CACHE.clear()
    sources._TOPJOBS_DETAIL_CACHE.clear()
    sources._XPRESS_DETAIL_CACHE.clear()
    yield
    sources._INVENTORY_CACHE.clear()
    sources._ITPRO_DETAIL_CACHE.clear()
    sources._TOPJOBS_DETAIL_CACHE.clear()
    sources._XPRESS_DETAIL_CACHE.clear()


def _itpro_card(
    job_id: str,
    title: str,
    company: str = "Example Co",
    location: str = "Colombo",
) -> str:
    return f"""
    <article class="job-card featured" id="{job_id}">
      <a href="https://itpro.lk/job/{job_id}/example/">
        <h2 class="jc-title">{title}</h2>
        <span class="jc-company">{company}</span>
        <span class="la"><svg></svg>{location}</span>
        <time class="time-posted" datetime="2026-07-28T10:00:00+05:30">today</time>
      </a>
    </article>
    """


def _topjobs_row(job_id: str, title: str, company: str = "Example Co") -> str:
    return f"""
    <tr id="tr7">
      <td>7</td>
      <td>{int(job_id)}</td>
      <td>
        <span id="hdnJC7">{job_id}</span>
        <span id="hdnEC7">0000000002</span>
        <span id="hdnAC7">0000000001</span>
        <h2><span>{title}</span></h2>
        <h1>{company}</h1>
      </td>
      <td>Please refer to the advert</td>
      <td>Tue Jul 28 2026</td>
      <!-- <td>commented opening date must not shift fields</td> -->
      <td>Tue Aug 11 2026</td>
      <td>Colombo 3</td>
    </tr>
    """


def test_itpro_discovers_every_live_category_without_role_selection():
    html = """
    <a href="/jobs/software-engineering/">Software Engineering</a>
    <a href="/jobs/quality-assurance/">Quality Assurance</a>
    <a href="/jobs/quality-assurance/fulltime/">QA full time</a>
    <a href="/rss/quality-assurance/">QA RSS</a>
    """

    assert _itpro_category_urls(html) == [
        "https://itpro.lk/jobs/software-engineering/",
        "https://itpro.lk/jobs/quality-assurance/",
    ]


def test_itpro_collects_all_categories_and_pages_before_local_filter(monkeypatch):
    index = """
    <a href="/jobs/software-engineering/">Software Engineering</a>
    <a href="/jobs/quality-assurance/">Quality Assurance</a>
    """
    software = (
        _itpro_card("1", "Backend Developer")
        + '<a href="/jobs/software-engineering/?p=2">2</a>'
    )
    software_page_2 = _itpro_card("2", "QA Automation Engineer")
    quality = _itpro_card("3", "Quality Assurance Engineer")
    requested = []

    def fake_get_text(url, **kwargs):
        requested.append(url)
        if url == ITProSriLankaProvider.feed_index:
            return index
        if url == "https://itpro.lk/jobs/software-engineering/":
            return software
        if "software-engineering/?p=2" in url:
            return software_page_2
        if url == "https://itpro.lk/jobs/quality-assurance/":
            return quality
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(sources, "_get_text", fake_get_text)
    profile = CandidateProfile("CV", target_position="QA Engineer")
    provider = ITProSriLankaProvider()

    jobs = provider.search(profile, "Sri Lanka", limit=1)

    assert [job.title for job in jobs] == ["QA Automation Engineer"]
    assert provider.last_inventory_count == 3
    assert "https://itpro.lk/jobs/quality-assurance/" in requested
    assert any("software-engineering/?p=2" in url for url in requested)


def test_topjobs_dynamically_prefers_all_vacancies_over_role_categories():
    base = TopJobsSriLankaProvider.directory_endpoint
    html = """
    <a href="vacancybyfunctionalarea.jsp?FA=NEW&jst=OPEN">A new category</a>
    <a href="vacancybyfunctionalarea.jsp?FA=ANOTHER&jst=OPEN">Another category</a>
    <a href="vacancybyfunctionalarea.jsp?jst=OPEN">All Vacancies</a>
    """

    assert _topjobs_inventory_roots(html, base) == [base]


def test_topjobs_follows_inventory_pagination_then_filters_locally(monkeypatch):
    base = TopJobsSriLankaProvider.directory_endpoint
    page_2 = (
        "https://www.topjobs.lk/applicant/vacancybyfunctionalarea.jsp"
        "?FA=&jst=OPEN&pageNo=2"
    )
    first = (
        '<a href="vacancybyfunctionalarea.jsp?FA=SDQ&jst=OPEN">IT</a>'
        '<a href="vacancybyfunctionalarea.jsp?jst=OPEN">All Vacancies</a>'
        f'<a href="{page_2}">2</a>'
        + _topjobs_row("0001000001", "Sales Executive")
    )
    second = _topjobs_row("0001000002", "Management Accountant")
    requested = []

    def fake_get_text(url, **kwargs):
        requested.append(url)
        if url == base:
            return first
        if url == page_2:
            return second
        if url.startswith("https://www.topjobs.lk/vacancy?"):
            return '<div id="remark"><img src="/advert.jpg" alt=""></div>'
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(sources, "_get_text", fake_get_text)
    profile = CandidateProfile("CV", target_position="Accountant")
    provider = TopJobsSriLankaProvider()

    jobs = provider.search(profile, "Sri Lanka", limit=1)

    assert [job.title for job in jobs] == ["Management Accountant"]
    assert provider.last_inventory_count == 2
    assert jobs[0].company == "Example Co"
    assert jobs[0].location == "Colombo 3"
    assert jobs[0].published_at == "Tue Jul 28 2026"
    assert requested[:2] == [base, page_2]
    assert len(requested) == 3


def test_xpress_uses_unfiltered_record_count_pagination_before_role_filter(monkeypatch):
    provider = XpressJobsSriLankaProvider()
    provider.page_size = 2
    calls = []
    pages = {
        1: [
            {
                "jobId": 1,
                "jobTitle": "Sales Executive",
                "organizationName": "A",
                "recordCount": 3,
            },
            {
                "jobId": 2,
                "jobTitle": "Finance Intern",
                "organizationName": "B",
                "recordCount": 3,
            },
        ],
        2: [
            {
                "jobId": 3,
                "jobTitle": "Management Accountant",
                "organizationName": "C",
                "recordCount": 3,
            }
        ],
    }

    def fake_get_json_list(url, params):
        calls.append((url, params.copy()))
        return pages[int(params["page"])]

    monkeypatch.setattr(sources, "_get_json_list", fake_get_json_list)
    monkeypatch.setattr(
        sources,
        "_enrich_xpress_jobs",
        lambda jobs, max_workers: jobs,
    )
    profile = CandidateProfile("CV", target_position="Accountant")

    jobs = provider.search(profile, "Sri Lanka", limit=1)

    assert [job.title for job in jobs] == ["Management Accountant"]
    assert provider.last_inventory_count == 3
    assert [params["page"] for _, params in calls] == ["1", "2"]
    assert all(params["keyword"] == "" for _, params in calls)
    assert all("Accountant" not in str(params) for _, params in calls)
    assert parse_qs(urlparse(jobs[0].url).query) == {}


def test_xpress_excludes_foreign_and_expired_rows_and_preserves_dates(monkeypatch):
    provider = XpressJobsSriLankaProvider()
    provider.page_size = 10
    payload = [
        {
            "jobId": 1,
            "jobTitle": "Registered Nurse",
            "organizationName": "Foreign Recruiter",
            "locations": "Foreign Job, International",
            "expiryDateOnWebsite": "2999-01-01T00:00:00",
            "recordCount": 3,
        },
        {
            "jobId": 2,
            "jobTitle": "Accountant",
            "organizationName": "Expired Co",
            "locations": "Colombo, Western Province",
            "expiryDateOnWebsite": "2000-01-01T00:00:00",
            "recordCount": 3,
        },
        {
            "jobId": 3,
            "jobTitle": "Accountant",
            "organizationName": "Current Co",
            "locations": "Colombo, Western Province",
            "overview": "Prepare monthly accounts.",
            "createdDate": "2026-07-28T09:30:00+05:30",
            "expiryDateOnWebsite": "2999-01-01T00:00:00",
            "recordCount": 3,
        },
    ]
    monkeypatch.setattr(sources, "_get_json_list", lambda *args, **kwargs: payload)

    jobs = provider._load_inventory()

    assert [job.source_id for job in jobs] == ["3"]
    assert jobs[0].published_at == "2026-07-28T09:30:00+05:30"
    assert "Closing date: 2999-01-01T00:00:00" in jobs[0].description


def test_xpress_expiry_keeps_the_current_sri_lanka_calendar_day():
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    assert _xpress_expiry_is_past("2026-07-27T00:00:00", now)
    assert not _xpress_expiry_is_past("2026-07-28T00:00:00", now)
    assert _xpress_is_foreign_location("Foreign Job, International")
    assert not _xpress_is_foreign_location("Colombo International Airport")


def test_xpress_detail_enrichment_preserves_full_text_and_posted_date(monkeypatch):
    jobs = [
        Job(
            "XpressJobs",
            "123",
            "Software Engineer",
            "Example",
            "Colombo",
            "sri lanka",
            "https://xpress.jobs/jobs/view/123",
            "Overview. Closing date: 2026-08-11T00:00:00",
        ),
        Job(
            "XpressJobs",
            "456",
            "Software Engineer",
            "Fallback",
            "Colombo",
            "sri lanka",
            "https://xpress.jobs/jobs/view/456",
            "Fallback overview.",
        ),
    ]

    def fake_get_json(url, params):
        if params["jobId"] == "456":
            raise OSError("detail unavailable")
        return {
            "jobInfo": """
                <p>Build production Python services and REST APIs.</p>
                <ul><li>Docker</li><li>AWS</li></ul>
            """,
            "education": "Bachelor's Degree",
            "experience": "2 years",
            "benefits": "Medical insurance",
            "createdDate": "2026-07-28T03:56:27.747",
        }

    monkeypatch.setattr(sources, "_get_json", fake_get_json)

    enriched = _enrich_xpress_jobs(jobs, max_workers=2)

    assert "production Python services" in enriched[0].description
    assert "Closing date: 2026-08-11T00:00:00" in enriched[0].description
    assert "Education: Bachelor's Degree" in enriched[0].description
    assert enriched[0].published_at == "2026-07-28T03:56:27.747"
    assert enriched[1] == jobs[1]


def test_itpro_detail_enrichment_is_concurrent_safe_and_failure_isolated(monkeypatch):
    jobs = [
        Job(
            "ITPro.lk",
            "1",
            "DevOps Engineer",
            "A",
            "Colombo",
            "sri lanka",
            "https://itpro.lk/job/1/devops-engineer/",
            "card fallback one",
        ),
        Job(
            "ITPro.lk",
            "2",
            "DevOps Engineer",
            "B",
            "Galle",
            "sri lanka",
            "https://itpro.lk/job/2/devops-engineer/",
            "card fallback two",
        ),
    ]

    def fake_get_text(url, **kwargs):
        if "/job/2/" in url:
            raise OSError("detail unavailable")
        return """
        <section id="job-description">
          Build AWS infrastructure with Terraform, Kubernetes and CI/CD pipelines.
        </section>
        """

    monkeypatch.setattr(sources, "_get_text", fake_get_text)

    enriched = _enrich_itpro_jobs(jobs, max_workers=2)

    assert "Terraform" in enriched[0].description
    assert enriched[1].description == "card fallback two"
    assert [job.source_id for job in enriched] == ["1", "2"]


def test_topjobs_detail_enrichment_keeps_image_only_fallback(monkeypatch):
    jobs = [
        Job(
            "topjobs.lk",
            "1",
            "DevOps Engineer",
            "A",
            "Colombo",
            "sri lanka",
            "https://www.topjobs.lk/vacancy?jc=1",
            "listing fallback one Closing date: Tue Aug 11 2026",
        ),
        Job(
            "topjobs.lk",
            "2",
            "Accountant",
            "B",
            "Colombo",
            "sri lanka",
            "https://www.topjobs.lk/vacancy?jc=2",
            "listing fallback two Closing date: Tue Aug 11 2026",
        ),
    ]

    def fake_get_text(url, **kwargs):
        if "jc=2" in url:
            return '<div id="remark"><p><img src="/advert.jpg" alt=""></p></div>'
        return """
        <div id="remark">
          <p>Design and operate AWS cloud infrastructure for production systems.</p>
          <ul><li>Terraform</li><li>Kubernetes</li><li>CI/CD</li></ul>
        </div>
        """

    monkeypatch.setattr(sources, "_get_text", fake_get_text)

    enriched = _enrich_topjobs_jobs(jobs, max_workers=2)

    assert "Terraform" in enriched[0].description
    assert "Closing date: Tue Aug 11 2026" in enriched[0].description
    assert enriched[1].description == jobs[1].description


def test_structured_sort_prefers_exact_role_then_recency():
    jobs = [
        Job(
            "Test",
            "loose",
            "Platform Engineer",
            "A",
            "Colombo",
            "sri lanka",
            "https://example.lk/loose",
            "",
            "2026-07-28",
        ),
        Job(
            "Test",
            "exact-old",
            "DevOps Engineer",
            "B",
            "Colombo",
            "sri lanka",
            "https://example.lk/exact-old",
            "",
            "2026-07-20",
        ),
        Job(
            "Test",
            "senior-new",
            "Senior DevOps Engineer",
            "C",
            "Colombo",
            "sri lanka",
            "https://example.lk/senior-new",
            "",
            "2026-07-27",
        ),
    ]

    ranked = _sort_structured_jobs(jobs, "DevOps Engineer")

    assert [job.source_id for job in ranked] == [
        "exact-old",
        "senior-new",
        "loose",
    ]


@pytest.mark.parametrize(
    ("target", "title"),
    [
        ("Software Engineer", "Full Stack Developer"),
        ("Software Engineer", "Application Engineer"),
        ("Accountant", "Accounting Executive"),
        ("QA Engineer", "Quality Assurance Analyst"),
        ("Registered Nurse", "Staff Nurse"),
        ("Cybersecurity Analyst", "SOC Analyst"),
        ("HR Executive", "Talent Acquisition Specialist"),
    ],
)
def test_role_gate_includes_deterministic_family_variants(target, title):
    assert _target_role_matches(title, target)


@pytest.mark.parametrize(
    ("target", "title"),
    [
        ("Accountant", "Sales Executive"),
        ("Registered Nurse", "Software Engineer"),
        ("Software Engineer", "Mechanical Engineer"),
    ],
)
def test_role_gate_rejects_unrelated_titles(target, title):
    assert not _target_role_matches(title, target)


def test_position_variants_are_dynamic_for_non_technical_roles():
    assert "accounting" in _position_variants("Accountant")
    assert "staff nurse" in _position_variants("Registered Nurse")


def test_inventory_cache_reuses_loader_and_returns_an_independent_list():
    key = "test:inventory-cache-reuse"
    calls = []
    inventory = [
        Job(
            "Test Inventory",
            "1",
            "DevOps Engineer",
            "Example",
            "Colombo",
            "sri lanka",
            "https://example.lk/jobs/1",
            "Current vacancy",
        )
    ]

    def loader():
        calls.append("loaded")
        return inventory

    sources._INVENTORY_CACHE.pop(key, None)
    try:
        first = _cached_inventory(key, loader)
        first.clear()
        second = _cached_inventory(key, loader)
    finally:
        sources._INVENTORY_CACHE.pop(key, None)
        sources._INVENTORY_LOCKS.pop(key, None)

    assert calls == ["loaded"]
    assert second == inventory
    assert second is not inventory


def test_complete_cached_inventory_is_reused_across_different_positions(
    monkeypatch,
):
    cache_key = "sri-lanka:itpro"
    provider = ITProSriLankaProvider()
    calls = []
    inventory = [
        Job(
            provider.name,
            "devops",
            "DevOps Engineer",
            "Example",
            "Colombo",
            "sri lanka",
            "https://itpro.lk/job/devops",
            "Cloud infrastructure",
        ),
        Job(
            provider.name,
            "accountant",
            "Management Accountant",
            "Example",
            "Colombo",
            "sri lanka",
            "https://itpro.lk/job/accountant",
            "Finance operations",
        ),
    ]

    def loader():
        calls.append("loaded")
        return inventory

    monkeypatch.setattr(provider, "_load_inventory", loader)
    monkeypatch.setattr(
        sources,
        "_enrich_itpro_jobs",
        lambda jobs, max_workers: jobs,
    )
    sources._INVENTORY_CACHE.pop(cache_key, None)
    try:
        devops_jobs = provider.search(
            CandidateProfile("CV", target_position="DevOps Engineer"),
            "Sri Lanka",
            limit=10,
        )
        accounting_jobs = provider.search(
            CandidateProfile("CV", target_position="Accountant"),
            "Sri Lanka",
            limit=10,
        )
    finally:
        sources._INVENTORY_CACHE.pop(cache_key, None)
        sources._INVENTORY_LOCKS.pop(cache_key, None)

    assert calls == ["loaded"]
    assert [job.source_id for job in devops_jobs] == ["devops"]
    assert [job.source_id for job in accounting_jobs] == ["accountant"]
