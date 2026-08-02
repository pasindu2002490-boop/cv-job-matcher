from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import asyncio
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Callable, Iterable
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from .country import adzuna_country_code, normalize_country
from .models import CandidateProfile, Job

logger = logging.getLogger(__name__)

_INVENTORY_CACHE: dict[str, tuple[datetime, list[Job]]] = {}
_INVENTORY_LOCKS: dict[str, threading.Lock] = {}
_INVENTORY_LOCKS_GUARD = threading.Lock()
_ITPRO_DETAIL_CACHE: dict[str, tuple[datetime, str]] = {}
_ITPRO_DETAIL_CACHE_LOCK = threading.Lock()
_TOPJOBS_DETAIL_CACHE: dict[str, tuple[datetime, str]] = {}
_TOPJOBS_DETAIL_CACHE_LOCK = threading.Lock()
_XPRESS_DETAIL_CACHE: dict[str, tuple[datetime, str, str]] = {}
_XPRESS_DETAIL_CACHE_LOCK = threading.Lock()
_HTTP_GET_CACHE: OrderedDict[str, tuple[float, str, int]] = OrderedDict()
_HTTP_GET_INFLIGHT: dict[str, "_HttpGetFlight"] = {}
_HTTP_GET_CACHE_LOCK = threading.Lock()
_HTTP_GET_CACHE_BYTES = 0
_HTTP_GET_CACHE_MAX_TTL_SECONDS = 60 * 60
_HTTP_GET_CACHE_HARD_MAX_ENTRIES = 2048
_HTTP_GET_CACHE_HARD_MAX_BYTES = 128 * 1024 * 1024


class _HttpGetFlight:
    """One in-progress GET shared by callers with identical request semantics."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: str | None = None
        self.error: BaseException | None = None


def _cached_inventory(key: str, loader: Callable[[], list[Job]]) -> list[Job]:
    """Reuse expensive complete inventories across user runs for a bounded TTL."""
    ttl_minutes = max(1, int(os.getenv("SOURCE_INVENTORY_CACHE_MINUTES", "30")))
    with _INVENTORY_LOCKS_GUARD:
        lock = _INVENTORY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = datetime.now(timezone.utc)
        cached = _INVENTORY_CACHE.get(key)
        if cached and now - cached[0] <= timedelta(minutes=ttl_minutes):
            logger.info("Source inventory cache hit: %s (%d rows)", key, len(cached[1]))
            return list(cached[1])
        inventory = loader()
        _INVENTORY_CACHE[key] = (now, list(inventory))
        logger.info("Source inventory cache refreshed: %s (%d rows)", key, len(inventory))
        return list(inventory)


USER_AGENT = "cv-job-matcher/0.1 (+local candidate matching tool)"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SEARCH_SKILL_PRIORITY = (
    "machine learning",
    "python",
    "llm",
    "nlp",
    "rag",
    "mlops",
    "langchain",
    "data engineering",
    "fastapi",
    "postgresql",
    "docker",
    "kubernetes",
    "aws",
    "azure",
    "react",
    "typescript",
    "javascript",
    "sql",
)
AI_ML_QUERY_TERMS = (
    "machine learning engineer",
    "ai engineer",
    "ai/ml engineer",
    "ml engineer",
    "data scientist",
    "llm engineer",
    "rag engineer",
    "mlops engineer",
    "computer vision engineer",
    "nlp engineer",
    "python machine learning",
)
SRI_LANKA_SEARCH_DOMAINS = (
    "topjobs.lk",
    "xpress.jobs",
    "ikman.lk",
    "jobber.lk",
    "jobfactory.lk",
    "dreamjobs.lk",
    "jobeka.lk",
    "findmyjob.lk",
    "lk.linkedin.com/jobs",
    "career141.com",
    "careerfirst.lk",
    "observerjobs.lk",
    "timesjobs.lk",
    "governmentjobs.lk",
    "governmentvacancies.lk",
    "gazette.lk",
    "job.govdoc.lk",
    "slbfe.lk",
    "lankaqualityjobs.com",
    "recruitme.lk",
    "jobpal.lk",
    "jobup.lk",
    "myjobs.lk",
    "itpro.lk",
    "careerlk.com",
    "hire.lk",
    "recruiter.lk",
    "lankajob.lk",
    "inseeks.com",
    "jobs.observer.lk",
    "cse.lk",
    "gov.lk",
)

SRI_LANKA_PORTALS = (
    ("CareerLK", "https://careerlk.com/jobs/"),
    ("Hire.lk", "https://www.hire.lk/jobs"),
    ("Recruiter.lk", "https://www.recruiter.lk/jobs"),
    ("LankaJob.lk", "https://lankajob.lk/"),
    ("Jobber.lk", "https://jobber.lk/"),
    ("JobFactory.lk", "https://www.jobfactory.lk/"),
    ("DreamJobs.lk", "https://www.dreamjobs.lk/"),
    ("JobEka.lk", "https://jobeka.lk/"),
    ("FindMyJob.lk", "https://findmyjob.lk/"),
    ("Career141", "https://www.career141.com/"),
    ("TimesJobs.lk", "https://timesjobs.lk/"),
    ("GovernmentJobs.lk", "https://governmentjobs.lk/"),
    ("GovernmentVacancies.lk", "https://www.governmentvacancies.lk/"),
    ("Gazette.lk", "https://www.gazette.lk/jobs"),
    ("job.govdoc.lk", "https://job.govdoc.lk/"),
    ("SLBFE Job Bank", "https://jobbank.slbfe.lk/"),
    ("LankaQualityJobs.com", "http://www.lankaqualityjobs.com/"),
    ("Recruitme.lk", "https://recruitme.lk/"),
    ("Jobup.lk", "https://jobup.lk/"),
    ("MYJOBS.LK", "https://myjobs.lk/"),
    ("Inseeks", "https://www.inseeks.com/"),
    ("Observer Jobs", "https://jobs.observer.lk/"),
    ("JobPal", "https://jobpal.lk/local-jobs/"),
    ("Ikman Jobs", "https://ikman.lk/en/ads/sri-lanka/jobs"),
    ("CareerFirst", "https://www.careerfirst.lk/"),
    ("CSE Careers", "https://www.cse.lk/"),
    ("Government Jobs", "https://www.gov.lk/"),
)


class JobProvider:
    name = "base"
    is_remote_global = False
    is_search_discovery = False

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        raise NotImplementedError

    @property
    def disabled_reason(self) -> str:
        return ""


class RemotiveProvider(JobProvider):
    name = "Remotive"
    is_remote_global = True
    endpoint = "https://remotive.com/api/remote-jobs"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        jobs = []
        target = normalize_country(country)
        per_query = max(5, min(limit, 50))
        for query in _search_queries(profile):
            payload = _get_json(self.endpoint, {"search": query, "limit": str(per_query)})
            for item in payload.get("jobs", []):
                location = str(item.get("candidate_required_location") or "")
                if not _remote_location_matches(location, target):
                    continue
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=str(item.get("id") or item.get("url") or ""),
                        title=str(item.get("title") or ""),
                        company=str(item.get("company_name") or ""),
                        location=location or "Remote",
                        country_hint=target,
                        url=str(item.get("url") or ""),
                        description=_clean_html(str(item.get("description") or "")),
                        published_at=str(item.get("publication_date") or ""),
                        salary=str(item.get("salary") or ""),
                        job_type=str(item.get("job_type") or ""),
                    )
                )
        return _dedupe_jobs(jobs)[:limit]


class HimalayasProvider(JobProvider):
    name = "Himalayas"
    is_remote_global = True
    endpoint = "https://himalayas.app/jobs/api/search"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        jobs = []
        target = normalize_country(country)
        for query in _search_queries(profile):
            payload = _get_json(self.endpoint, {"q": query, "sort": "recent", "page": "1"})
            for item in payload.get("jobs", []):
                location = _himalayas_location(item.get("locationRestrictions"))
                if not _remote_location_matches(location, target):
                    continue
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=str(item.get("guid") or item.get("applicationLink") or ""),
                        title=str(item.get("title") or ""),
                        company=str(item.get("companyName") or ""),
                        location=location,
                        country_hint=target,
                        url=str(item.get("applicationLink") or ""),
                        description=_clean_html(
                            " ".join(str(item.get(key) or "") for key in ("excerpt", "description"))
                        ),
                        published_at=_date_to_iso(item.get("pubDate")),
                        salary=_himalayas_salary(item),
                        job_type=str(item.get("employmentType") or ""),
                    )
                )
        return _dedupe_jobs(jobs)[:limit]


class RemoteOkProvider(JobProvider):
    name = "Remote OK"
    is_remote_global = True
    endpoint = "https://remoteok.com/api"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        payload = _get_json_list(self.endpoint)
        target = normalize_country(country)
        wanted = [term.lower() for term in _search_queries(profile)]
        jobs = []
        for item in payload:
            if not isinstance(item, dict) or item.get("legal"):
                continue
            haystack = " ".join(
                [
                    str(item.get("position") or ""),
                    str(item.get("description") or ""),
                    " ".join(str(tag) for tag in item.get("tags") or []),
                ]
            ).lower()
            if not _matches_any_query(haystack, wanted):
                continue
            location = str(item.get("location") or "Worldwide")
            if not _remote_location_matches(location, target):
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id") or item.get("slug") or item.get("url") or ""),
                    title=str(item.get("position") or ""),
                    company=str(item.get("company") or ""),
                    location=location,
                    country_hint=target,
                    url=str(item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id') or ''}"),
                    description=_clean_html(str(item.get("description") or "")),
                    published_at=str(item.get("date") or ""),
                    salary=str(item.get("salary") or ""),
                    job_type="remote",
                )
            )
        return _dedupe_jobs(jobs)[:limit]


class WeWorkRemotelyProvider(JobProvider):
    name = "We Work Remotely"
    is_remote_global = True
    endpoints = (
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
    )

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        target = normalize_country(country)
        wanted = [term.lower() for term in _search_queries(profile)]
        jobs = []
        for endpoint in self.endpoints:
            root = _get_xml(endpoint)
            for item in root.findall("./channel/item"):
                title = _xml_text(item, "title")
                description = _clean_html(_xml_text(item, "description"))
                location = _xml_text(item, "region") or "Remote"
                haystack = f"{title} {description} {_xml_text(item, 'category')}".lower()
                if not _matches_any_query(haystack, wanted):
                    continue
                if not _remote_location_matches(location, target):
                    continue
                company, role = _split_wwr_title(title)
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=_xml_text(item, "guid") or _xml_text(item, "link"),
                        title=role,
                        company=company,
                        location=location,
                        country_hint=target,
                        url=_xml_text(item, "link"),
                        description=description,
                        published_at=_xml_text(item, "pubDate"),
                        salary="",
                        job_type=_xml_text(item, "category"),
                    )
                )
        return _dedupe_jobs(jobs)[:limit]


class ArbeitnowProvider(JobProvider):
    name = "Arbeitnow"
    endpoint = "https://www.arbeitnow.com/api/job-board-api"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        target = normalize_country(country)
        if target not in {"germany", "deutschland"}:
            return []
        payload = _get_json(self.endpoint, {"page": "1"})
        jobs = []
        query_terms = set(_query_terms(profile))
        for item in payload.get("data", []):
            haystack = " ".join(
                str(item.get(key) or "") for key in ("title", "description", "company_name", "location")
            ).lower()
            if query_terms and not any(term.lower() in haystack for term in query_terms):
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("slug") or item.get("url") or ""),
                    title=str(item.get("title") or ""),
                    company=str(item.get("company_name") or ""),
                    location=str(item.get("location") or "Germany"),
                    country_hint="germany",
                    url=str(item.get("url") or ""),
                    description=_clean_html(str(item.get("description") or "")),
                    published_at=_unix_to_iso(item.get("created_at")),
                    salary="",
                    job_type="remote" if item.get("remote") else "",
                )
            )
            if len(jobs) >= limit:
                break
        return jobs


class ITProSriLankaProvider(JobProvider):
    name = "ITPro.lk"
    feed_index = "https://itpro.lk/rss/"
    all_jobs_feed = "https://itpro.lk/rss/all/"
    max_pages_per_category = 100
    detail_workers = 6

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        inventory = _cached_inventory("sri-lanka:itpro", self._load_inventory)
        self.last_inventory_count = len(inventory)
        jobs = [
            job
            for job in inventory
            if _structured_role_matches(profile, job.title)
        ]
        ranked = _sort_structured_jobs(jobs, profile.target_position)
        return _enrich_itpro_jobs(ranked[:limit], self.detail_workers)

    def _load_inventory(self) -> list[Job]:
        index_html = _get_text(self.feed_index, headers=BROWSER_HEADERS)
        category_urls = _itpro_category_urls(index_html)
        if not category_urls:
            raise ValueError("ITPro category inventory could not be discovered")

        inventory: list[Job] = []
        for category_url in category_urls:
            pending = [category_url]
            visited: set[str] = set()
            while pending:
                page_url = pending.pop(0)
                if page_url in visited:
                    continue
                if len(visited) >= self.max_pages_per_category:
                    raise ValueError(f"ITPro pagination exceeded safety limit for {category_url}")
                visited.add(page_url)
                html = _get_text(page_url, headers=BROWSER_HEADERS)
                inventory.extend(_itpro_jobs_from_html(html))
                for discovered in _itpro_pagination_urls(html, category_url):
                    if discovered not in visited and discovered not in pending:
                        pending.append(discovered)
        return _dedupe_jobs(inventory)


def _itpro_category_urls(index_html: str) -> list[str]:
    """Return every current top-level ITPro category from the live directory."""
    categories = []
    for _, url in _html_links(index_html, "https://itpro.lk/rss/"):
        parsed = urlparse(url)
        if parsed.netloc.lower().removeprefix("www.") != "itpro.lk":
            continue
        if not re.fullmatch(r"/jobs/[^/]+", parsed.path.rstrip("/")):
            continue
        categories.append(parsed._replace(path=parsed.path.rstrip("/") + "/").geturl())
    return list(dict.fromkeys(categories))


def _itpro_pagination_urls(html: str, category_url: str) -> list[str]:
    category = urlparse(category_url)
    pages = []
    for _, url in _html_links(html, category_url):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if (
            parsed.netloc.lower().removeprefix("www.") == "itpro.lk"
            and parsed.path.rstrip("/") == category.path.rstrip("/")
            and query.get("p", [""])[0].isdigit()
            and int(query["p"][0]) > 1
        ):
            pages.append(parsed._replace(path=parsed.path.rstrip("/") + "/").geturl())
    return list(dict.fromkeys(pages))


def _itpro_jobs_from_html(html: str) -> list[Job]:
    jobs = []
    for match in re.finditer(
        r'<article\b(?=[^>]*class=["\'][^"\']*\bjob-card\b)[^>]*>.*?</article>',
        html,
        flags=re.I | re.S,
    ):
        card = match.group(0)
        url = unescape(_first_group(card, r'<a\b[^>]*href=["\']([^"\']+)'))
        title = _clean_html(
            _first_group(card, r'<h2\b[^>]*class=["\'][^"\']*\bjc-title\b[^"\']*["\'][^>]*>(.*?)</h2>')
        )
        if not url or not title:
            continue
        company = _clean_html(
            _first_group(card, r'<span\b[^>]*class=["\'][^"\']*\bjc-company\b[^"\']*["\'][^>]*>(.*?)</span>')
        )
        location = _clean_html(
            _first_group(card, r'<span\b[^>]*class=["\'][^"\']*\bla\b[^"\']*["\'][^>]*>(.*?)</span>')
        )
        source_id = _first_group(card, r'<article\b[^>]*\bid=["\']([^"\']+)') or url
        published_at = _first_group(card, r'<time\b[^>]*\bdatetime=["\']([^"\']+)')
        jobs.append(
            Job(
                source=ITProSriLankaProvider.name,
                source_id=source_id,
                title=title,
                company=company,
                location=location or "Sri Lanka",
                country_hint="sri lanka",
                url=urljoin("https://itpro.lk/", url),
                description=_clean_html(card),
                published_at=published_at,
                salary="",
                job_type="",
            )
        )
    return jobs


def _itpro_detail_description(html: str) -> str:
    section = _first_group(
        html,
        r'<section\b[^>]*\bid=["\']job-description["\'][^>]*>(.*?)</section>',
    )
    description = _clean_html(section)
    if description:
        return description
    for match in re.finditer(
        r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        try:
            payload = json.loads(unescape(match.group(1)))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("@type") or "").lower() != "jobposting":
            continue
        description = _clean_html(str(payload.get("description") or ""))
        if description:
            return description
    return ""


def _itpro_detail_description_for_job(job: Job) -> str:
    parsed = urlparse(job.url)
    if (
        parsed.netloc.lower().removeprefix("www.") != "itpro.lk"
        or not parsed.path.startswith("/job/")
    ):
        return ""
    now = datetime.now(timezone.utc)
    ttl = timedelta(
        minutes=max(1, int(os.getenv("SOURCE_INVENTORY_CACHE_MINUTES", "30")))
    )
    with _ITPRO_DETAIL_CACHE_LOCK:
        cached = _ITPRO_DETAIL_CACHE.get(job.url)
        if cached and now - cached[0] <= ttl:
            return cached[1]
    html = _get_text(job.url, headers=BROWSER_HEADERS, timeout=20)
    description = _itpro_detail_description(html)
    if description:
        with _ITPRO_DETAIL_CACHE_LOCK:
            _ITPRO_DETAIL_CACHE[job.url] = (now, description)
    return description


def _enrich_itpro_jobs(jobs: list[Job], max_workers: int = 6) -> list[Job]:
    """Fetch selected detail pages concurrently while preserving card fallbacks."""
    if not jobs:
        return []
    enriched = list(jobs)
    worker_count = max(1, min(max_workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_itpro_detail_description_for_job, job): index
            for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                description = future.result()
            except Exception as exc:
                logger.warning(
                    "ITPro detail enrichment failed for %s: %s",
                    jobs[index].url,
                    exc,
                )
                continue
            if description:
                enriched[index] = replace(jobs[index], description=description)
    return enriched


def _itpro_feed_urls(index_html: str, target_position: str) -> list[str]:
    """Discover current ITPro feeds instead of hardcoding a role category."""
    discovered = []
    for title, url in _html_links(index_html, "https://itpro.lk/rss/"):
        parsed = urlparse(url)
        if parsed.netloc.lower().removeprefix("www.") != "itpro.lk":
            continue
        if not parsed.path.startswith("/rss/") or parsed.path == "/rss/":
            continue
        if parsed.path.rstrip("/") == "/rss/all" or _target_role_matches(
            title, target_position
        ):
            discovered.append(parsed._replace(path=parsed.path.rstrip("/") + "/").geturl())
    if "https://itpro.lk/rss/all/" not in discovered:
        discovered.insert(0, "https://itpro.lk/rss/all/")
    return list(dict.fromkeys(discovered))


class TopJobsSriLankaProvider(JobProvider):
    name = "topjobs.lk"
    directory_endpoint = "https://www.topjobs.lk/applicant/vacancybyfunctionalarea.jsp?jst=OPEN"
    max_pages_per_inventory = 100
    detail_workers = 6

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        inventory = _cached_inventory("sri-lanka:topjobs", self._load_inventory)
        self.last_inventory_count = len(inventory)
        jobs = [
            job
            for job in inventory
            if _structured_role_matches(profile, job.title)
        ]
        ranked = _sort_structured_jobs(jobs, profile.target_position)
        return _enrich_topjobs_jobs(ranked[:limit], self.detail_workers)

    def _load_inventory(self) -> list[Job]:
        first_html = _get_text(
            self.directory_endpoint,
            headers=BROWSER_HEADERS,
            encoding="iso-8859-1",
        )
        pending = _topjobs_inventory_roots(first_html, self.directory_endpoint)
        if not pending:
            raise ValueError("topjobs inventory routes could not be discovered")

        inventory: list[Job] = []
        visited: set[str] = set()
        first_used = False
        while pending:
            endpoint = pending.pop(0)
            if endpoint in visited:
                continue
            if len(visited) >= self.max_pages_per_inventory:
                raise ValueError("topjobs pagination exceeded safety limit")
            visited.add(endpoint)
            if endpoint == self.directory_endpoint and not first_used:
                html = first_html
                first_used = True
            else:
                html = _get_text(endpoint, headers=BROWSER_HEADERS, encoding="iso-8859-1")
            inventory.extend(_topjobs_jobs_from_html(html, endpoint))
            for page_url in _topjobs_pagination_urls(html, endpoint):
                if page_url not in visited and page_url not in pending:
                    pending.append(page_url)
        return _dedupe_jobs(inventory)


def _topjobs_category_urls(index_html: str, base_url: str) -> list[str]:
    categories = []
    base = urlparse(base_url)
    for _, url in _html_links(index_html, base_url):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if (
            parsed.netloc.lower().removeprefix("www.") == base.netloc.lower().removeprefix("www.")
            and parsed.path == base.path
            and query.get("jst", [""])[0].upper() == "OPEN"
            and "pageNo" not in query
        ):
            categories.append(url)
    return list(dict.fromkeys(categories))


def _topjobs_inventory_roots(index_html: str, base_url: str) -> list[str]:
    """Prefer the complete live inventory; use discovered categories as fallback."""
    categories = _topjobs_category_urls(index_html, base_url)
    all_vacancies = [
        url for url in categories if not parse_qs(urlparse(url).query).get("FA", [""])[0]
    ]
    if all_vacancies:
        return [all_vacancies[0]]
    return categories


def _topjobs_pagination_urls(html: str, inventory_url: str) -> list[str]:
    inventory = urlparse(inventory_url)
    inventory_fa = parse_qs(inventory.query).get("FA", [""])[0]
    pages = []
    for _, url in _html_links(html, inventory_url):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if (
            parsed.netloc.lower().removeprefix("www.")
            == inventory.netloc.lower().removeprefix("www.")
            and parsed.path == inventory.path
            and query.get("pageNo", [""])[0].isdigit()
            and int(query["pageNo"][0]) > 1
            and query.get("FA", [""])[0] == inventory_fa
        ):
            pages.append(url)
    return list(dict.fromkeys(pages))


def _topjobs_jobs_from_html(html: str, endpoint: str) -> list[Job]:
    jobs = []
    for match in re.finditer(r"<tr\b.*?</tr>", html, flags=re.I | re.S):
        row = match.group(0)
        title = _clean_html(_first_group(row, r"<h2>\s*<span>(.*?)</span>\s*</h2>"))
        if not title:
            continue
        company = _clean_html(_first_group(row, r"<h1>(.*?)</h1>"))
        jc = _hidden_value(row, "hdnJC")
        ec = _hidden_value(row, "hdnEC") or "DEFZZZ"
        ac = _hidden_value(row, "hdnAC") or "DEFZZZ"
        rid = _first_group(row, r'<tr\b[^>]*\bid=["\']tr(\d+)') or _row_number(row)
        url = _topjobs_url(ac, ec, jc, rid) if jc else endpoint
        visible_row = re.sub(r"<!--.*?-->", "", row, flags=re.S)
        cells = [
            _clean_html(cell)
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", visible_row, flags=re.I | re.S)
        ]
        published = cells[-3] if len(cells) >= 3 else ""
        closing = cells[-2] if len(cells) >= 2 else ""
        location = cells[-1] if cells else "Sri Lanka"
        jobs.append(
            Job(
                source=TopJobsSriLankaProvider.name,
                source_id=jc or url,
                title=title,
                company=company,
                location=location or "Sri Lanka",
                country_hint="sri lanka",
                url=url,
                description=f"{_clean_html(visible_row)} Closing date: {closing}",
                published_at=published,
                salary="",
                job_type="",
            )
        )
    return jobs


def _topjobs_detail_description(html: str) -> str:
    remark = _first_group(
        html,
        r'<div\b[^>]*\bid=["\']remark["\'][^>]*>(.*?)</div>',
    )
    description = _clean_html(remark)
    generic = (
        "please refer the full details of the job posting",
        "please refer the vacancy",
        "please refer the advert",
    )
    if len(description) < 80 or any(
        description.lower().startswith(prefix) for prefix in generic
    ):
        return ""
    return description


def _topjobs_detail_description_for_job(job: Job) -> str:
    parsed = urlparse(job.url)
    if (
        parsed.netloc.lower().removeprefix("www.") != "topjobs.lk"
        or parsed.path != "/vacancy"
    ):
        return ""
    now = datetime.now(timezone.utc)
    ttl = timedelta(
        minutes=max(1, int(os.getenv("SOURCE_INVENTORY_CACHE_MINUTES", "30")))
    )
    with _TOPJOBS_DETAIL_CACHE_LOCK:
        cached = _TOPJOBS_DETAIL_CACHE.get(job.url)
        if cached and now - cached[0] <= ttl:
            return cached[1]
    html = _get_text(job.url, headers=BROWSER_HEADERS, timeout=20)
    description = _topjobs_detail_description(html)
    if description:
        with _TOPJOBS_DETAIL_CACHE_LOCK:
            _TOPJOBS_DETAIL_CACHE[job.url] = (now, description)
    return description


def _enrich_topjobs_jobs(jobs: list[Job], max_workers: int = 6) -> list[Job]:
    """Add extractable advert text while retaining image/PDF listing evidence."""
    if not jobs:
        return []
    enriched = list(jobs)
    worker_count = max(1, min(max_workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_topjobs_detail_description_for_job, job): index
            for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                description = future.result()
            except Exception as exc:
                logger.warning(
                    "topjobs detail enrichment failed for %s: %s",
                    jobs[index].url,
                    exc,
                )
                continue
            if description:
                enriched[index] = replace(
                    jobs[index],
                    description=f"{description} {jobs[index].description}",
                )
    return enriched


class XpressJobsSriLankaProvider(JobProvider):
    name = "XpressJobs"
    endpoint = "https://xpress.jobs/api/jobs/searchJobs"
    detail_endpoint = "https://xpress.jobs/api/jobs/publishedJob"
    page_size = 100
    max_pages = 250
    detail_workers = 6

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        inventory = _cached_inventory("sri-lanka:xpressjobs", self._load_inventory)
        self.last_inventory_count = len(inventory)
        jobs = [
            job
            for job in inventory
            if _structured_role_matches(profile, job.title)
        ]
        ranked = _sort_structured_jobs(jobs, profile.target_position)
        return _enrich_xpress_jobs(ranked[:limit], self.detail_workers)

    def _load_inventory(self) -> list[Job]:
        inventory: list[Job] = []
        page = 1
        total_pages: int | None = None
        fetched_at = datetime.now(timezone.utc)
        while page <= self.max_pages:
            payload = _get_json_list(
                self.endpoint,
                {
                    "page": str(page),
                    "pageSize": str(self.page_size),
                    "keyword": "",
                    "sortBy": "SortedCreateDate DESC",
                },
            )
            if not payload:
                break
            for item in payload:
                location = str(item.get("locations") or "").strip()
                if _xpress_is_foreign_location(location):
                    continue
                expiry = str(item.get("expiryDateOnWebsite") or "").strip()
                if expiry and _xpress_expiry_is_past(expiry, fetched_at):
                    continue
                job_id = str(item.get("jobId") or "")
                published_at = str(
                    item.get("createdDate")
                    or item.get("publishedDate")
                    or item.get("sortedCreateDate")
                    or ""
                )
                description_parts = [
                    _clean_html(str(item.get("overview") or "")),
                ]
                if published_at:
                    description_parts.append(f"Posted: {published_at}")
                if expiry:
                    description_parts.append(f"Closing date: {expiry}")
                inventory.append(
                    Job(
                        source=self.name,
                        source_id=job_id,
                        title=str(item.get("jobTitle") or ""),
                        company=str(item.get("organizationName") or ""),
                        location=location or "Sri Lanka",
                        country_hint="sri lanka",
                        url=f"https://xpress.jobs/jobs/view/{job_id}" if job_id else "https://xpress.jobs/jobs",
                        description=" ".join(part for part in description_parts if part),
                        published_at=published_at,
                        salary="",
                        job_type=str(item.get("jobType") or ""),
                    )
                )
            record_counts = []
            for item in payload:
                try:
                    record_counts.append(int(item.get("recordCount") or 0))
                except (TypeError, ValueError):
                    continue
            if record_counts:
                discovered_pages = (max(record_counts) + self.page_size - 1) // self.page_size
                total_pages = max(total_pages or 0, discovered_pages)
                if total_pages > self.max_pages:
                    raise ValueError(
                        f"XpressJobs reported {total_pages} pages, above safety limit"
                    )
            if total_pages is not None and page >= total_pages:
                break
            if total_pages is None and len(payload) < self.page_size:
                break
            page += 1
        return _dedupe_jobs(inventory)


def _xpress_is_foreign_location(location: str) -> bool:
    lower = location.lower()
    if "foreign job" in lower:
        return True
    segments = [
        segment.strip()
        for segment in re.split(r"[,;|]", lower)
        if segment.strip()
    ]
    return "international" in segments


def _xpress_expiry_is_past(
    value: str,
    now: datetime | None = None,
) -> bool:
    try:
        expiry = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    if expiry.tzinfo is None and expiry.time() == datetime.min.time():
        sri_lanka_date = (current.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)).date()
        return expiry.date() < sri_lanka_date
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return expiry.astimezone(timezone.utc) < current.astimezone(timezone.utc)


def _xpress_detail_for_job(job: Job) -> tuple[str, str]:
    if not job.source_id.isdigit():
        return "", ""
    now = datetime.now(timezone.utc)
    ttl = timedelta(
        minutes=max(1, int(os.getenv("SOURCE_INVENTORY_CACHE_MINUTES", "30")))
    )
    with _XPRESS_DETAIL_CACHE_LOCK:
        cached = _XPRESS_DETAIL_CACHE.get(job.source_id)
        if cached and now - cached[0] <= ttl:
            return cached[1], cached[2]
    payload = _get_json(
        XpressJobsSriLankaProvider.detail_endpoint,
        {"jobId": job.source_id},
    )
    if not isinstance(payload, dict):
        return "", ""
    description_parts = [
        _clean_html(str(payload.get("jobInfo") or "")),
    ]
    education = _clean_html(str(payload.get("education") or ""))
    experience = _clean_html(str(payload.get("experience") or ""))
    benefits = _clean_html(str(payload.get("benefits") or ""))
    if education:
        description_parts.append(f"Education: {education}")
    if experience:
        description_parts.append(f"Experience: {experience}")
    if benefits:
        description_parts.append(f"Benefits: {benefits}")
    detail_description = " ".join(part for part in description_parts if part)
    published_at = str(payload.get("createdDate") or "")
    if detail_description or published_at:
        with _XPRESS_DETAIL_CACHE_LOCK:
            _XPRESS_DETAIL_CACHE[job.source_id] = (
                now,
                detail_description,
                published_at,
            )
    return detail_description, published_at


def _enrich_xpress_jobs(jobs: list[Job], max_workers: int = 6) -> list[Job]:
    """Fetch full public job records with bounded, failure-isolated concurrency."""
    if not jobs:
        return []
    enriched = list(jobs)
    worker_count = max(1, min(max_workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_xpress_detail_for_job, job): index
            for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                description, published_at = future.result()
            except Exception as exc:
                logger.warning(
                    "XpressJobs detail enrichment failed for %s: %s",
                    jobs[index].url,
                    exc,
                )
                continue
            updates = {}
            if description:
                updates["description"] = f"{description} {jobs[index].description}"
            if published_at:
                updates["published_at"] = published_at
            if updates:
                enriched[index] = replace(jobs[index], **updates)
    return enriched


def _structured_role_matches(profile: CandidateProfile, title: str) -> bool:
    if profile.target_position:
        return _target_role_matches(title, profile.target_position)
    if profile.likely_titles:
        return any(_target_role_matches(title, role) for role in profile.likely_titles)
    return True


def job_title_matches_profile(profile: CandidateProfile, title: str) -> bool:
    """Public deterministic gate used for a shared, role-agnostic inventory."""
    return _structured_role_matches(profile, title)


def _sort_structured_jobs(jobs: list[Job], target_position: str) -> list[Job]:
    return sorted(
        jobs,
        key=lambda job: (
            _structured_title_relevance(job.title, target_position),
            _structured_published_timestamp(job.published_at),
        ),
        reverse=True,
    )


def _structured_title_relevance(title: str, target_position: str) -> tuple[int, float]:
    target = re.sub(r"[^a-z0-9+#]+", " ", target_position.lower()).strip()
    normalized_title = re.sub(r"[^a-z0-9+#]+", " ", title.lower()).strip()
    if not target:
        return (0, 0.0)
    if normalized_title == target:
        return (4, 1.0)
    if re.search(rf"\b{re.escape(target)}\b", normalized_title):
        return (3, 1.0)
    target_tokens = [
        token
        for token in target.split()
        if token not in {"junior", "senior", "lead"}
    ]
    if not target_tokens:
        return (0, 0.0)
    overlap = sum(
        bool(re.search(rf"\b{re.escape(token)}\b", normalized_title))
        for token in target_tokens
    )
    coverage = overlap / len(target_tokens)
    return (2 if overlap == len(target_tokens) else 1 if overlap else 0, coverage)


def _structured_published_timestamp(value: str) -> float:
    raw = value.strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for date_format in ("%a %b %d %Y", "%Y-%m-%d", "%d %b %Y"):
            try:
                parsed = datetime.strptime(raw, date_format)
                break
            except ValueError:
                continue
        if parsed is None:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class RemoteRocketshipProvider(JobProvider):
    name = "RemoteRocketship"
    is_remote_global = True

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        jobs = []
        positions = [profile.target_position, *_position_variants(profile.target_position)]
        if not any(positions):
            positions = list(profile.likely_titles) or ["software engineer"]
        endpoints = [
            "https://www.remoterocketship.com/country/sri-lanka/jobs/"
            f"{re.sub(r'[^a-z0-9]+', '-', position.lower()).strip('-')}/"
            for position in dict.fromkeys(position for position in positions if position)
        ]
        for endpoint in endpoints:
            try:
                html = _get_text(endpoint, headers=BROWSER_HEADERS)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                logger.warning("%s blocked or unavailable: %s: %s", self.name, endpoint, exc)
                continue
            for item in _json_ld_items(html):
                url = str(item.get("url") or "")
                title = str(item.get("name") or item.get("title") or "")
                if not title and url:
                    title = _title_from_slug(url)
                if not url or not _is_ai_ml_title(f"{title} {url}", profile):
                    continue
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=url,
                        title=title or "Machine Learning / AI role",
                        company=str((item.get("hiringOrganization") or {}).get("name") or ""),
                        location="Sri Lanka / Remote",
                        country_hint="sri lanka",
                        url=url,
                        description=str(item.get("description") or title),
                        published_at=str(item.get("datePosted") or ""),
                        salary="",
                        job_type=str(item.get("employmentType") or ""),
                    )
                )
        return _dedupe_jobs(jobs)[:limit]


class LinkedInPublicSriLankaProvider(JobProvider):
    name = "LinkedIn Public"
    base_queries = (
        "AI Engineer",
        "Artificial Intelligence Engineer",
        "Artificial Intelligence",
        "AI ML Engineer",
        "Machine Learning Engineer",
        "ML Engineer",
        "Generative AI Engineer",
        "Gen AI Engineer",
        "LLM Engineer",
        "MLOps Engineer",
        "NLP Engineer",
        "Computer Vision Engineer",
        "Data Scientist",
        "AI Developer",
        "AI Architect",
    )

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        jobs = []
        for endpoint in self._endpoints(profile):
            # LinkedIn's public guest endpoint is paginated in blocks of 25.
            # Stop only after a page has no cards, rather than silently reading
            # just the first page for every search phrase.
            for start in range(0, min(max(limit, 25), 200), 25):
                try:
                    html = _get_text(f"{endpoint}?start={start}", headers=BROWSER_HEADERS, timeout=12)
                except HTTPError as exc:
                    if exc.code == 429 and jobs:
                        return _dedupe_jobs(jobs)[:limit]
                    raise
                except (URLError, TimeoutError, OSError):
                    if jobs:
                        return _dedupe_jobs(jobs)[:limit]
                    raise
                page_jobs = 0
                for match in re.finditer(r'<div class="base-card\b.*?</li>', html, flags=re.I | re.S):
                    row = match.group(0)
                    title = _clean_html(_first_group(row, r'<h3 class="base-search-card__title">\s*(.*?)\s*</h3>'))
                    company = _clean_html(
                        _first_group(row, r'<h4 class="base-search-card__subtitle">.*?<a[^>]*>\s*(.*?)\s*</a>')
                    )
                    location = _clean_html(
                        _first_group(row, r'<span class="job-search-card__location">\s*(.*?)\s*</span>')
                    )
                    url = unescape(_first_group(row, r'<a class="base-card__full-link[^"]*" href="([^"]+)"'))
                    published = _first_group(row, r'<time[^>]*datetime="([^"]+)"')
                    if not title or not url or not _is_ai_ml_title(title, profile):
                        continue
                    if "sri lanka" not in location.lower():
                        continue
                    jobs.append(
                        Job(
                            source=self.name,
                            source_id=_first_group(row, r"urn:li:jobPosting:(\d+)") or url,
                            title=title,
                            company=company,
                            location=location or "Sri Lanka",
                            country_hint="sri lanka",
                            url=url,
                            description=title,
                            published_at=published,
                            salary="",
                            job_type="",
                        )
                    )
                    page_jobs += 1
                if page_jobs == 0:
                    break
        return _dedupe_jobs(jobs)[:limit]

    def _endpoints(self, profile: CandidateProfile) -> list[str]:
        queries = _search_queries(profile) if profile.target_position else list(self.base_queries)
        endpoints = []
        for query in dict.fromkeys(q for q in queries if q):
            slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
            if slug:
                endpoints.append(f"https://lk.linkedin.com/jobs/{slug}-jobs")
        if not profile.target_position:
            endpoints.append("https://lk.linkedin.com/jobs/artificial-intelligence-ai-jobs")
        return list(dict.fromkeys(endpoints))


class DuckDuckGoDiscoveryProvider(JobProvider):
    name = "DuckDuckGo Discovery"
    endpoint = "https://html.duckduckgo.com/html/"
    is_search_discovery = True

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if limit <= 0:
            return []
        query_cap = _search_discovery_query_cap()
        candidate_cap = _search_discovery_candidate_cap(limit)
        candidates: list[Job] = []
        for query in self._queries(profile, country)[:query_cap]:
            try:
                html = _post_text(
                    self.endpoint,
                    {"q": query},
                    headers={
                        "User-Agent": BROWSER_HEADERS["User-Agent"],
                        "Accept": "text/html,application/xhtml+xml",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=20,
                )
            except (HTTPError, URLError, TimeoutError, OSError):
                if candidates:
                    break
                raise
            for item in _duckduckgo_results(html):
                title = item["title"]
                url = item["url"]
                snippet = item["snippet"]
                if not url or not _looks_like_job_url_or_text(f"{title} {snippet} {url}", profile):
                    continue
                candidates.append(
                    _search_result_job(self.name, title, url, snippet, country)
                )
                candidates = _dedupe_jobs(candidates)
                if len(candidates) >= candidate_cap:
                    break
            if len(candidates) >= candidate_cap:
                break
        return _validate_search_discovery_candidates(
            candidates,
            profile,
            country,
            limit,
        )

    def _queries(self, profile: CandidateProfile, country: str) -> list[str]:
        position = profile.target_position or "software engineer"
        base = [
            f'"{position}" "{country}" apply job',
            f'"{position}" "{country}" vacancy',
        ]
        for variant in _position_variants(position):
            base.append(f'"{variant}" "{country}" job vacancy apply')
        if country == "sri lanka":
            for domain in SRI_LANKA_SEARCH_DOMAINS:
                base.append(f'site:{domain} "{position}" "Sri Lanka"')
                variants = _position_variants(position)[:3]
                if variants:
                    joined = " OR ".join(f'"{variant}"' for variant in variants)
                    base.append(f"site:{domain} ({joined})")
        return list(dict.fromkeys(base))


class GoogleCustomSearchProvider(DuckDuckGoDiscoveryProvider):
    name = "Google Custom Search"
    endpoint = "https://www.googleapis.com/customsearch/v1"
    is_search_discovery = True

    @property
    def disabled_reason(self) -> str:
        if not os.getenv("GOOGLE_CSE_API_KEY", "").strip() and not os.getenv("GOOGLE_API_KEY", "").strip():
            return "set GOOGLE_CSE_API_KEY or GOOGLE_API_KEY"
        if not os.getenv("GOOGLE_CSE_ID", "").strip():
            return "set GOOGLE_CSE_ID"
        return ""

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if self.disabled_reason or limit <= 0:
            return []
        api_key = os.getenv("GOOGLE_CSE_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        cx = os.getenv("GOOGLE_CSE_ID", "").strip()
        query_cap = _search_discovery_query_cap()
        candidate_cap = _search_discovery_candidate_cap(limit)
        candidates: list[Job] = []
        for query in self._queries(profile, country)[:query_cap]:
            try:
                payload = _get_json(
                    self.endpoint,
                    {"key": api_key, "cx": cx, "q": query, "num": "10"},
                )
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                logger.warning("%s blocked or unavailable for query %s: %s", self.name, query, exc)
                if candidates:
                    break
                return []
            for item in payload.get("items", []):
                title = str(item.get("title") or "")
                url = str(item.get("link") or "")
                snippet = str(item.get("snippet") or "")
                if not url or not _looks_like_job_url_or_text(f"{title} {snippet} {url}", profile):
                    continue
                candidates.append(_search_result_job(self.name, title, url, snippet, country))
                candidates = _dedupe_jobs(candidates)
                if len(candidates) >= candidate_cap:
                    break
            if len(candidates) >= candidate_cap:
                break
        return _validate_search_discovery_candidates(
            candidates,
            profile,
            country,
            limit,
        )


class SerpApiGoogleProvider(DuckDuckGoDiscoveryProvider):
    name = "SerpAPI Google"
    endpoint = "https://serpapi.com/search.json"
    is_search_discovery = True

    @property
    def disabled_reason(self) -> str:
        return "" if os.getenv("SERPAPI_API_KEY", "").strip() else "set SERPAPI_API_KEY"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if self.disabled_reason or limit <= 0:
            return []
        api_key = os.getenv("SERPAPI_API_KEY", "").strip()
        query_cap = _search_discovery_query_cap()
        candidate_cap = _search_discovery_candidate_cap(limit)
        candidates: list[Job] = []
        for query in self._queries(profile, country)[:query_cap]:
            try:
                payload = _get_json(
                    self.endpoint,
                    {
                        "engine": "google",
                        "api_key": api_key,
                        "q": query,
                        "location": country.title(),
                        "num": "20",
                    },
                )
            except (
                HTTPError,
                URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
                ValueError,
            ):
                if candidates:
                    break
                raise
            for item in payload.get("organic_results", []):
                title = str(item.get("title") or "")
                url = str(item.get("link") or "")
                snippet = str(item.get("snippet") or "")
                if not url or not _looks_like_job_url_or_text(f"{title} {snippet} {url}", profile):
                    continue
                candidates.append(_search_result_job(self.name, title, url, snippet, country))
                candidates = _dedupe_jobs(candidates)
                if len(candidates) >= candidate_cap:
                    break
            if len(candidates) >= candidate_cap:
                break
        return _validate_search_discovery_candidates(
            candidates,
            profile,
            country,
            limit,
        )


class Crawl4AiSeedProvider(JobProvider):
    name = "Crawl4AI Seeds"
    is_search_discovery = True
    max_discovery_pages = 18
    max_detail_pages = 80

    @property
    def disabled_reason(self) -> str:
        if os.getenv("CRAWL4AI_ENABLED", "").strip() != "1":
            return "set CRAWL4AI_ENABLED=1 and CRAWL4AI_SEED_URLS"
        if not os.getenv("CRAWL4AI_SEED_URLS", "").strip():
            return "set CRAWL4AI_SEED_URLS"
        try:
            import crawl4ai  # noqa: F401
        except ImportError:
            return "install crawl4ai"
        return ""

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if self.disabled_reason:
            return []
        seeds = [
            url.strip()
            for url in os.getenv("CRAWL4AI_SEED_URLS", "").split(",")
            if url.strip()
        ]
        return asyncio.run(self._crawl(seeds, profile, country, limit))

    async def _crawl(
        self,
        seeds: list[str],
        profile: CandidateProfile,
        country: str,
        limit: int,
    ) -> list[Job]:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        if not seeds or limit <= 0:
            return []
        crawl_urls: list[tuple[str, str]] = []
        for seed in seeds:
            origin = _site_host(seed)
            for url in dict.fromkeys(
                (_crawl4ai_query_url(seed, profile.target_position), seed)
            ):
                if origin and _same_site(url, seed):
                    crawl_urls.append((_canonical_crawl_url(url), origin))
        browser = BrowserConfig(headless=True, verbose=False)
        run_config = CrawlerRunConfig(
            page_timeout=30000,
            delay_before_return_html=1.0,
            scan_full_page=True,
            remove_overlay_elements=True,
            check_robots_txt=True,
            verbose=False,
        )
        async with AsyncWebCrawler(config=browser) as crawler:
            queue = list(dict.fromkeys(crawl_urls))
            navigation_labels = {
                url: profile.target_position if url != _canonical_crawl_url(seed) else "All jobs"
                for seed in seeds
                for url in (
                    _canonical_crawl_url(_crawl4ai_query_url(seed, profile.target_position)),
                    _canonical_crawl_url(seed),
                )
            }
            visited: set[str] = set()
            candidates: dict[str, tuple[str, str]] = {}
            successful_listings = 0
            discovery_cap = min(
                self.max_discovery_pages,
                max(6, limit * 2),
            )
            candidate_cap = min(
                self.max_detail_pages,
                max(16, limit * 5),
            )

            while queue and len(visited) < discovery_cap:
                batch: list[tuple[str, str]] = []
                while queue and len(batch) < 4 and len(visited) + len(batch) < discovery_cap:
                    url, origin = queue.pop(0)
                    if url in visited or any(item[0] == url for item in batch):
                        continue
                    batch.append((url, origin))
                if not batch:
                    break
                requested_urls = [item[0] for item in batch]
                listing_results = await crawler.arun_many(
                    requested_urls,
                    config=run_config,
                    max_concurrency=min(4, len(requested_urls)),
                )
                for requested_url in requested_urls:
                    visited.add(requested_url)
                for result in listing_results:
                    page_url = _canonical_crawl_url(str(result.url or ""))
                    matched_request = next(
                        (
                            (requested_url, request_origin)
                            for requested_url, request_origin in batch
                            if page_url == requested_url
                        ),
                        None,
                    )
                    if matched_request is None:
                        matched_request = next(
                            (
                                (requested_url, request_origin)
                                for requested_url, request_origin in batch
                                if page_url and _site_host(page_url) == request_origin
                            ),
                            None,
                        )
                    if matched_request is None:
                        logger.warning(
                            "Crawl4AI returned an unrequested discovery result: %s",
                            page_url,
                        )
                        continue
                    requested_url, origin = matched_request
                    if not result.success:
                        logger.warning(
                            "Crawl4AI discovery failed: %s: %s",
                            requested_url,
                            result.error_message,
                        )
                        continue
                    successful_listings += 1
                    page_url = page_url or requested_url
                    html = str(result.html or "")
                    discovered_links = _crawl4ai_result_links(result, page_url)
                    _collect_structured_job_links(
                        html,
                        page_url,
                        origin,
                        candidates,
                        candidate_cap,
                    )
                    for title, url in discovered_links:
                        if _site_host(url) != origin:
                            continue
                        if _portal_navigation_link(url, title):
                            if (
                                url not in visited
                                and not any(item[0] == url for item in queue)
                                and len(queue) < discovery_cap * 3
                            ):
                                queued = (url, origin)
                                queue.append(queued)
                                navigation_labels.setdefault(url, title)
                            continue
                        if _potential_job_detail_link(url, title):
                            candidates.setdefault(url, (title, origin))
                    for search_term in _portal_search_terms(profile):
                        for search_url in _portal_search_form_urls(
                            html,
                            page_url,
                            search_term,
                        ):
                            if (
                                _site_host(search_url) == origin
                                and search_url not in visited
                                and not any(item[0] == search_url for item in queue)
                                and len(queue) < discovery_cap * 3
                            ):
                                queue.append((search_url, origin))
                                navigation_labels.setdefault(
                                    search_url,
                                    f"Search jobs {search_term}",
                                )
                    queue.sort(
                        key=lambda queued: _portal_navigation_priority(
                            queued[0],
                            navigation_labels.get(queued[0], ""),
                            profile,
                        )
                    )

            if successful_listings == 0:
                raise OSError("Crawl4AI could not load any configured discovery page")

            ordered_candidates = _prioritize_job_candidates(candidates, profile)
            detail_urls = ordered_candidates[:candidate_cap]
            if not detail_urls:
                return []
            details = await crawler.arun_many(
                detail_urls,
                config=run_config,
                max_concurrency=min(6, len(detail_urls)),
            )

        jobs: list[Job] = []
        detail_by_clean_url = {_clean_url(url): url for url in detail_urls}
        for result in details:
            result_url = _canonical_crawl_url(str(result.url or ""))
            url = detail_by_clean_url.get(_clean_url(result_url))
            if not url:
                logger.warning(
                    "Crawl4AI returned an unrequested detail result: %s",
                    result_url,
                )
                continue
            if not result.success:
                logger.warning(
                    "Crawl4AI detail failed: %s: %s",
                    url,
                    result.error_message,
                )
                continue
            fallback_title, _ = candidates[url]
            job = _portal_job_from_page(
                source=self.name,
                url=url,
                html=str(result.html or ""),
                fallback_title=fallback_title,
                profile=profile,
                country=country,
                rendered_text=str(result.markdown or ""),
            )
            if job is not None:
                jobs.append(job)
                if len(jobs) >= limit:
                    break
        return _dedupe_jobs(jobs)[:limit]


def _crawl4ai_query_url(seed_url: str, position: str) -> str:
    if not position:
        return seed_url
    host = urlparse(seed_url).netloc.lower()
    if "hire.lk" in host:
        return f"https://www.hire.lk/jobs?{urlencode({'q': position})}"
    if "careerlk.com" in host or "jobpal.lk" in host:
        separator = "&" if "?" in seed_url else "?"
        return f"{seed_url}{separator}{urlencode({'search_keywords': position})}"
    if "recruiter.lk" in host:
        separator = "&" if "?" in seed_url else "?"
        return f"{seed_url}{separator}{urlencode({'search': position})}"
    return seed_url


class SriLankaPortalProvider(JobProvider):
    """Bounded inventory crawler for public Sri Lankan job portals."""

    max_discovery_pages = 18
    max_detail_pages = 80
    max_detail_workers = 6

    def __init__(self, name: str, seed_url: str) -> None:
        self.name = name
        self.seed_url = seed_url

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka" or limit <= 0:
            return []
        origin = _site_host(self.seed_url)
        if not origin:
            return []
        query_url = _canonical_crawl_url(
            _portal_query_url(self.seed_url, profile.target_position)
        )
        seed_url = _canonical_crawl_url(self.seed_url)
        queue = list(dict.fromkeys((query_url, seed_url)))
        navigation_labels = {
            query_url: profile.target_position,
            seed_url: "All jobs",
        }
        visited: set[str] = set()
        candidates: dict[str, tuple[str, str]] = {}
        successful_listings = 0
        last_error: Exception | None = None
        discovery_cap = min(self.max_discovery_pages, max(6, limit * 2))
        candidate_cap = min(self.max_detail_pages, max(16, limit * 5))

        while queue and len(visited) < discovery_cap:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            html, fetch_error = self._fetch_page(page_url)
            if html is None:
                last_error = fetch_error
                continue
            successful_listings += 1
            _collect_structured_job_links(
                html,
                page_url,
                origin,
                candidates,
                candidate_cap,
            )
            for title, url in _html_links(html, page_url):
                url = _canonical_crawl_url(url)
                if _site_host(url) != origin:
                    continue
                if _portal_navigation_link(url, title):
                    if (
                        url not in visited
                        and url not in queue
                        and len(queue) < discovery_cap * 3
                    ):
                        queue.append(url)
                        navigation_labels.setdefault(url, title)
                    continue
                if _potential_job_detail_link(url, title):
                    candidates.setdefault(url, (title, origin))
            for search_term in _portal_search_terms(profile):
                for search_url in _portal_search_form_urls(
                    html,
                    page_url,
                    search_term,
                ):
                    if (
                        _site_host(search_url) == origin
                        and search_url not in visited
                        and search_url not in queue
                        and len(queue) < discovery_cap * 3
                    ):
                        queue.append(search_url)
                        navigation_labels.setdefault(
                            search_url,
                            f"Search jobs {search_term}",
                        )
            queue.sort(
                key=lambda queued: _portal_navigation_priority(
                    queued,
                    navigation_labels.get(queued, ""),
                    profile,
                )
            )

        if successful_listings == 0 and last_error is not None:
            logger.warning("%s could not load any discovery page: %s", self.name, last_error)
            return []

        jobs: list[Job] = []
        detail_failures: list[tuple[str, Exception]] = []
        prioritized = _prioritize_job_candidates(candidates, profile)[:candidate_cap]
        worker_count = max(1, min(self.max_detail_workers, len(prioritized)))
        for batch_start in range(0, len(prioritized), worker_count):
            batch = prioritized[batch_start : batch_start + worker_count]
            batch_jobs: dict[int, Job] = {}
            batch_failures: list[tuple[str, Exception]] = []
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {}
                for offset, url in enumerate(batch):
                    fallback_title, _ = candidates[url]
                    future = executor.submit(
                        _fetch_portal_detail_job,
                        source=self.name,
                        url=url,
                        fallback_title=fallback_title,
                        profile=profile,
                        country="sri lanka",
                    )
                    futures[future] = (offset, url)
                for future in as_completed(futures):
                    offset, url = futures[future]
                    try:
                        job = future.result()
                    except Exception as exc:
                        batch_failures.append((url, exc))
                        detail_failures.append((url, exc))
                        continue
                    if job is not None:
                        batch_jobs[offset] = job
            for offset in range(len(batch)):
                if offset in batch_jobs:
                    jobs.append(batch_jobs[offset])
                    if len(jobs) >= limit:
                        break
            if len(jobs) >= limit:
                break
            # A whole batch of server-side failures means the portal's detail
            # service is unavailable. Do not hammer every remaining candidate.
            if len(batch) >= 3 and len(batch_failures) == len(batch) and all(
                isinstance(exc, HTTPError) and 500 <= exc.code < 600
                for _, exc in batch_failures
            ):
                break
        if detail_failures:
            first_url, first_error = detail_failures[0]
            logger.warning(
                "%s detail pages unavailable (%d attempted); first failure: %s: %s",
                self.name,
                len(detail_failures),
                first_url,
                first_error,
            )
        return _dedupe_jobs(jobs)[:limit]

    def _fetch_page(self, page_url: str) -> tuple[str | None, Exception | None]:
        attempts = [page_url]
        parsed = urlparse(page_url)
        if parsed.scheme.lower() == "https" and parsed.hostname:
            host = parsed.hostname.lower().removeprefix("www.")
            if host == "jobup.lk":
                attempts.append(parsed._replace(scheme="http").geturl())
        last_error: Exception | None = None
        for attempt_url in dict.fromkeys(attempts):
            for timeout in (15, 30):
                try:
                    return _get_text(attempt_url, headers=BROWSER_HEADERS, timeout=timeout), None
                except HTTPError as exc:
                    last_error = exc
                    if exc.code in {401, 403, 404, 410}:
                        break
                    break
                except (URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    continue
        return None, last_error


def _fetch_portal_detail_job(
    source: str,
    url: str,
    fallback_title: str,
    profile: CandidateProfile,
    country: str,
) -> Job | None:
    html = _get_text(url, headers=BROWSER_HEADERS, timeout=15)
    return _portal_job_from_page(
        source=source,
        url=url,
        html=html,
        fallback_title=fallback_title,
        profile=profile,
        country=country,
    )


class AdzunaProvider(JobProvider):
    name = "Adzuna"
    endpoint_root = "https://api.adzuna.com/v1/api/jobs"

    def __init__(self) -> None:
        self.app_id = os.getenv("ADZUNA_APP_ID", "").strip()
        self.app_key = os.getenv("ADZUNA_APP_KEY", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_key)

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        code = adzuna_country_code(country)
        if not code or not self.enabled:
            return []
        query = _build_query(profile)
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": str(limit),
            "what": query,
            "content-type": "application/json",
        }
        url = f"{self.endpoint_root}/{code}/search/1"
        payload = _get_json(url, params)
        jobs = []
        for item in payload.get("results", []):
            location_data = item.get("location") or {}
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id") or item.get("redirect_url") or ""),
                    title=str(item.get("title") or ""),
                    company=str((item.get("company") or {}).get("display_name") or ""),
                    location=str(location_data.get("display_name") or ""),
                    country_hint=normalize_country(country),
                    url=str(item.get("redirect_url") or ""),
                    description=_clean_html(str(item.get("description") or "")),
                    published_at=str(item.get("created") or ""),
                    salary=_salary(item),
                    job_type=str(item.get("contract_time") or item.get("contract_type") or ""),
                )
            )
        return jobs


def default_providers() -> list[JobProvider]:
    providers: list[JobProvider] = [
        AdzunaProvider(),
        RemotiveProvider(),
        HimalayasProvider(),
        RemoteOkProvider(),
        WeWorkRemotelyProvider(),
        ITProSriLankaProvider(),
        TopJobsSriLankaProvider(),
        XpressJobsSriLankaProvider(),
        RemoteRocketshipProvider(),
        LinkedInPublicSriLankaProvider(),
        DuckDuckGoDiscoveryProvider(),
        GoogleCustomSearchProvider(),
        SerpApiGoogleProvider(),
        Crawl4AiSeedProvider(),
        ArbeitnowProvider(),
    ]
    providers.extend(SriLankaPortalProvider(name, url) for name, url in SRI_LANKA_PORTALS)
    return providers


def search_all(
    profile: CandidateProfile,
    country: str,
    limit_per_source: int = 50,
    include_remote_global: bool = False,
    web_discovery: bool = False,
) -> tuple[list[Job], list[str]]:
    jobs: list[Job] = []
    notes: list[str] = []
    for provider in default_providers():
        logger.info("Source %s: starting", provider.name)
        if getattr(provider, "is_search_discovery", False) and not web_discovery:
            notes.append(f"{provider.name}: skipped (use --web-discovery to include search-engine discovery)")
            logger.info("Source %s: skipped (web discovery disabled)", provider.name)
            continue
        if provider.disabled_reason:
            notes.append(f"{provider.name}: skipped ({provider.disabled_reason})")
            logger.info("Source %s: skipped (%s)", provider.name, provider.disabled_reason)
            continue
        if provider.is_remote_global and not include_remote_global:
            notes.append(f"{provider.name}: skipped (use --include-remote-global to include worldwide remote boards)")
            logger.info("Source %s: skipped (remote sources disabled)", provider.name)
            continue
        try:
            result = provider.search(profile, country, limit_per_source)
            jobs.extend(result)
            status = f"{provider.name}: {len(result)} jobs"
            if isinstance(provider, AdzunaProvider) and not provider.enabled:
                status += " (disabled: set ADZUNA_APP_ID and ADZUNA_APP_KEY for broader country coverage)"
            notes.append(status)
            logger.info("Source %s: completed with %d jobs", provider.name, len(result))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            notes.append(f"{provider.name}: failed: {exc}")
            logger.warning("Source %s: failed: %s", provider.name, exc)
    return _dedupe_jobs(jobs), notes


def _bounded_http_cache_env_float(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(0.0, value), maximum)


def _bounded_http_cache_env_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(0, value), maximum)


def _http_get_cache_settings() -> tuple[float, int, int]:
    """Return TTL seconds, entry limit, and approximate in-memory byte limit."""
    ttl_minutes = _bounded_http_cache_env_float(
        "HTTP_GET_CACHE_MINUTES",
        5.0,
        _HTTP_GET_CACHE_MAX_TTL_SECONDS / 60.0,
    )
    ttl_seconds = ttl_minutes * 60.0
    max_entries = _bounded_http_cache_env_int(
        "HTTP_GET_CACHE_MAX_ENTRIES",
        512,
        _HTTP_GET_CACHE_HARD_MAX_ENTRIES,
    )
    max_bytes = _bounded_http_cache_env_int(
        "HTTP_GET_CACHE_MAX_BYTES",
        32 * 1024 * 1024,
        _HTTP_GET_CACHE_HARD_MAX_BYTES,
    )
    return ttl_seconds, max_entries, max_bytes


def _http_get_cache_key(
    url: str,
    headers: dict[str, str],
    encoding: str,
    timeout: int | float,
) -> str:
    """Hash all GET semantics without retaining signed URLs or auth headers.

    Timeout is intentionally part of the identity: a short-timeout caller must
    not be made to wait behind an otherwise identical long-timeout request.
    """
    normalized_headers = tuple(
        sorted((str(name).strip().lower(), str(value).strip()) for name, value in headers.items())
    )
    semantics = json.dumps(
        ("GET", url, normalized_headers, encoding.lower(), repr(float(timeout))),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(semantics.encode("utf-8")).hexdigest()


def _remove_http_get_cache_entry(key: str) -> None:
    global _HTTP_GET_CACHE_BYTES
    cached = _HTTP_GET_CACHE.pop(key, None)
    if cached:
        _HTTP_GET_CACHE_BYTES -= cached[2]


def _trim_http_get_cache(now: float, ttl_seconds: float, max_entries: int, max_bytes: int) -> None:
    expired = [
        key
        for key, (created_at, _, _) in _HTTP_GET_CACHE.items()
        if now - created_at > ttl_seconds
    ]
    for key in expired:
        _remove_http_get_cache_entry(key)
    while _HTTP_GET_CACHE and (
        len(_HTTP_GET_CACHE) > max_entries or _HTTP_GET_CACHE_BYTES > max_bytes
    ):
        oldest_key = next(iter(_HTTP_GET_CACHE))
        _remove_http_get_cache_entry(oldest_key)


def _clear_http_get_cache() -> None:
    """Clear completed GET responses; primarily useful for deterministic tests."""
    global _HTTP_GET_CACHE_BYTES
    with _HTTP_GET_CACHE_LOCK:
        if any(not flight.event.is_set() for flight in _HTTP_GET_INFLIGHT.values()):
            raise RuntimeError("Cannot clear HTTP GET cache while requests are in flight")
        _HTTP_GET_CACHE.clear()
        _HTTP_GET_INFLIGHT.clear()
        _HTTP_GET_CACHE_BYTES = 0


def _invalidate_http_get_cache(
    url: str,
    headers: dict[str, str],
    encoding: str,
    timeout: int | float,
) -> None:
    key = _http_get_cache_key(url, headers, encoding, timeout)
    with _HTTP_GET_CACHE_LOCK:
        _remove_http_get_cache_entry(key)


def _response_is_successful(response: object) -> bool:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    return status is None or 200 <= int(status) < 300


def _download_text(
    url: str,
    headers: dict[str, str],
    encoding: str,
    timeout: int | float,
) -> tuple[str, bool]:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        successful = _response_is_successful(response)
        text = response.read().decode(encoding, errors="replace")
    return text, successful


def _cached_get_text(
    url: str,
    headers: dict[str, str],
    encoding: str,
    timeout: int | float,
) -> str:
    """Read through a bounded LRU and collapse simultaneous identical GETs."""
    global _HTTP_GET_CACHE_BYTES

    ttl_seconds, max_entries, max_bytes = _http_get_cache_settings()
    if ttl_seconds <= 0 or max_entries <= 0 or max_bytes <= 0:
        return _download_text(url, headers, encoding, timeout)[0]

    key = _http_get_cache_key(url, headers, encoding, timeout)
    now = time.monotonic()
    with _HTTP_GET_CACHE_LOCK:
        _trim_http_get_cache(now, ttl_seconds, max_entries, max_bytes)
        cached = _HTTP_GET_CACHE.get(key)
        if cached:
            _HTTP_GET_CACHE.move_to_end(key)
            return cached[1]

        flight = _HTTP_GET_INFLIGHT.get(key)
        if flight is None:
            flight = _HttpGetFlight()
            _HTTP_GET_INFLIGHT[key] = flight
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        if flight.result is None:
            raise RuntimeError(f"HTTP GET single-flight completed without a result: {url}")
        return flight.result

    try:
        text, successful = _download_text(url, headers, encoding, timeout)
    except BaseException as exc:
        with _HTTP_GET_CACHE_LOCK:
            flight.error = exc
            _HTTP_GET_INFLIGHT.pop(key, None)
            flight.event.set()
        raise

    response_bytes = text.__sizeof__()
    with _HTTP_GET_CACHE_LOCK:
        flight.result = text
        if successful and response_bytes <= max_bytes:
            existing = _HTTP_GET_CACHE.get(key)
            if existing:
                _HTTP_GET_CACHE_BYTES -= existing[2]
            _HTTP_GET_CACHE[key] = (time.monotonic(), text, response_bytes)
            _HTTP_GET_CACHE.move_to_end(key)
            _HTTP_GET_CACHE_BYTES += response_bytes
            _trim_http_get_cache(
                time.monotonic(),
                ttl_seconds,
                max_entries,
                max_bytes,
            )
        _HTTP_GET_INFLIGHT.pop(key, None)
        flight.event.set()
    return text


def _get_json(url: str, params: dict[str, str] | None = None) -> dict:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    text = _get_text(full_url, headers=headers)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        _invalidate_http_get_cache(full_url, headers, "utf-8", 30)
        raise


def _get_json_list(url: str, params: dict[str, str] | None = None) -> list:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    text = _get_text(full_url, headers=headers)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        _invalidate_http_get_cache(full_url, headers, "utf-8", 30)
        raise
    return payload if isinstance(payload, list) else []


def _get_xml(url: str) -> ElementTree.Element:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, text/xml"}
    text = _get_text(url, headers=headers)
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        _invalidate_http_get_cache(url, headers, "utf-8", 30)
        raise


def _get_text(
    url: str,
    headers: dict[str, str] | None = None,
    encoding: str = "utf-8",
    timeout: int = 30,
) -> str:
    request_headers = headers or {"User-Agent": USER_AGENT, "Accept": "text/html"}
    return _cached_get_text(url, request_headers, encoding, timeout)


def _portal_query_url(seed_url: str, position: str) -> str:
    if not position:
        return seed_url
    host = urlparse(seed_url).netloc.lower()
    if "hire.lk" in host:
        return f"https://www.hire.lk/jobs?{urlencode({'q': position})}"
    if "careerlk.com" in host or "jobpal.lk" in host:
        separator = "&" if "?" in seed_url else "?"
        return f"{seed_url}{separator}{urlencode({'search_keywords': position})}"
    if "recruiter.lk" in host:
        separator = "&" if "?" in seed_url else "?"
        return f"{seed_url}{separator}{urlencode({'search': position})}"
    return seed_url


def _html_links(html: str, base_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in re.finditer(
        r"<a\b([^>]*)>(.*?)</a>",
        html,
        flags=re.I | re.S,
    ):
        attributes, body = match.groups()
        href = unescape(_html_attribute(attributes, "href")).strip()
        title = _clean_html(body)
        if not title:
            title = _clean_html(
                _html_attribute(attributes, "aria-label")
                or _html_attribute(attributes, "title")
                or _first_group(body, r"<img\b[^>]*\balt=[\"']([^\"']+)[\"']")
            )
        if (
            not href
            or href.lower().startswith(("#", "javascript:", "mailto:", "tel:", "data:"))
            or not title
        ):
            continue
        canonical = _canonical_crawl_url(urljoin(base_url, href))
        if canonical:
            links.append((title, canonical))
    return links


def _jobish_link(url: str, title: str) -> bool:
    parsed = urlparse(url)
    text = f" {parsed.path.lower()} {parsed.query.lower()} {title.lower()} "
    return any(
        term in text
        for term in (
            "job",
            "career",
            "vacanc",
            "position",
            "recruit",
            "opening",
            "opportunit",
            "apply",
        )
    )


def _generic_listing_title(title: str) -> bool:
    value = " ".join(title.lower().split()).strip(" -|:")
    generic = (
        "jobs",
        "browse jobs",
        "all jobs",
        "latest jobs",
        "local jobs",
        "find jobs",
        "find a job",
        "job vacancies",
        "vacancies",
        "careers",
        "career opportunities",
        "job search",
        "search",
        "search jobs",
        "search results",
        "view all",
        "see live jobs",
        "more jobs",
        "current openings",
        "open positions",
    )
    return value in generic or value.startswith(
        ("browse all", "view all", "search jobs", "find jobs", "all vacancies")
    )


def _listing_like_url(url: str) -> bool:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path).rstrip("/").lower()
    segments = [segment for segment in path.split("/") if segment]
    query_keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    detail_keys = {"job_id", "jobid", "vacancy_id", "vacancyid", "jid", "posting_id"}
    if query_keys.intersection(detail_keys):
        return False
    listing_query_keys = {
        "q",
        "query",
        "s",
        "search",
        "keyword",
        "keywords",
        "search_keywords",
        "search_query",
        "category",
        "sector",
        "industry",
        "department",
        "location",
        "page",
        "paged",
        "pageno",
        "start",
        "offset",
    }
    if query_keys.intersection(listing_query_keys):
        return True
    if not segments:
        return False
    listing_leaf_names = {
        "jobs",
        "job-search",
        "search-jobs",
        "careers",
        "vacancies",
        "openings",
        "positions",
        "local-jobs",
        "job-listings",
        "all-jobs",
        "latest-jobs",
    }
    if segments[-1] in listing_leaf_names:
        return True
    # Portal category indexes often use short slugs such as /it-jobs or
    # /jobs-in-colombo.  The word "job" alone must not make these detail ads.
    leaf = segments[-1]
    listing_slug = leaf.replace("_", "-").removesuffix(".php")
    if len(segments) == 1 and (
        listing_slug.endswith("-jobs")
        or "-jobs-in-" in listing_slug
        or listing_slug.startswith("jobs-in-")
        or listing_slug.startswith("government-job-vacanc")
    ):
        return True
    if segments == ["post", "job", "vacancy"]:
        return True
    listing_segments = {
        "category",
        "categories",
        "job-category",
        "job-categories",
        "sector",
        "sectors",
        "industry",
        "industries",
        "department",
        "departments",
        "browse",
        "search",
        "job-search",
        "tags",
        "tag",
    }
    if any(segment in listing_segments for segment in segments):
        return True
    return any(
        segment in {"page", "paged"} and index + 1 < len(segments)
        for index, segment in enumerate(segments)
    )


def _portal_title_matches(profile: CandidateProfile, title: str) -> bool:
    if profile.target_position:
        return _target_role_matches(title, profile.target_position)
    likely_titles = [term for term in profile.likely_titles if len(term) > 3]
    if likely_titles:
        title_lower = title.lower()
        return any(term.lower() in title_lower for term in likely_titles)
    return True


def _html_attribute(attributes: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        attributes,
        flags=re.I,
    )
    if not match:
        return ""
    return unescape(next((value for value in match.groups() if value is not None), ""))


def _site_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().strip(".")
    return host.removeprefix("www.")


def _same_site(url: str, seed_url: str) -> bool:
    host = _site_host(url)
    seed_host = _site_host(seed_url)
    return bool(
        host
        and seed_host
        and (
            host == seed_host
            or host.endswith(f".{seed_host}")
            or seed_host.endswith(f".{host}")
        )
    )


def _canonical_crawl_url(url: str) -> str:
    raw = unescape(str(url or "")).strip()
    if (
        not raw
        or any(ch.isspace() for ch in raw)
        or any(ch in raw for ch in ("`", '"', "'"))
        or re.search(r"https?:/{1,2}", raw[8:], flags=re.I)
    ):
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if key.lower().startswith("utm_") or key.lower() in {
            "fbclid",
            "gclid",
            "mc_cid",
            "mc_eid",
        }:
            continue
        query.extend((key, value) for value in values)
    netloc = parsed.netloc.lower()
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=netloc,
        path=path,
        query=urlencode(sorted(query)),
        fragment="",
    ).geturl()


def _is_homepage_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path.rstrip("/") == "" and not parsed.query


def _looks_like_listing_heading(title: str) -> bool:
    value = " ".join(title.lower().split())
    return (
        _generic_listing_title(value)
        or bool(re.search(r"\b(jobs|vacancies|openings)\s+in\b", value))
        or bool(re.search(r"\b(all|latest|current|available)\s+(jobs|vacancies|openings)\b", value))
        or any(
            phrase in value
            for phrase in ("job listings", "job search results", "browse vacancies")
        )
    )


def _portal_navigation_link(url: str, title: str) -> bool:
    if not url or _is_homepage_url(url):
        return False
    if _listing_like_url(url) or _looks_like_listing_heading(title):
        return True
    parsed = urlparse(url)
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    if (
        len(segments) == 2
        and segments[0] in {"jobs", "careers", "vacancies", "openings"}
        and _looks_like_job_category(title)
    ):
        return True
    label = " ".join(title.lower().split()).strip()
    if label in {"next", "previous", "prev", "older", "newer", "›", "»", "→"}:
        return True
    if re.fullmatch(r"(?:page\s*)?\d+", label):
        return True
    return any(
        phrase in label
        for phrase in (
            "job categories",
            "jobs by category",
            "jobs by sector",
            "jobs by industry",
            "browse by",
            "view more jobs",
            "load more jobs",
        )
    )


def _looks_like_job_category(title: str) -> bool:
    label = re.sub(r"[^a-z0-9&/+]+", " ", title.lower()).strip()
    if not label or _looks_like_role_title(label):
        return False
    category_terms = {
        "accounting",
        "administration",
        "agriculture",
        "apparel",
        "automotive",
        "banking",
        "beauty",
        "construction",
        "customer service",
        "education",
        "engineering",
        "finance",
        "healthcare",
        "hospitality",
        "human resources",
        "information technology",
        "legal",
        "logistics",
        "manufacturing",
        "marketing",
        "medical",
        "retail",
        "sales",
        "security",
        "supply chain",
        "technology",
        "tourism",
    }
    return "&" in label or "/" in label or any(
        re.search(rf"\b{re.escape(term)}\b", label)
        for term in category_terms
    )


def _portal_role_navigation_terms(profile: CandidateProfile) -> set[str]:
    """Map a role to broad portal vocabulary without relying on a site taxonomy."""
    raw_terms = [
        profile.target_position,
        *profile.likely_titles,
        *profile.skills,
    ]
    words = {
        token
        for value in raw_terms
        for token in re.findall(r"[a-z0-9+#]+", value.lower())
        if len(token) > 2
    }
    related: set[str] = set(words)
    families = (
        ({"accountant", "accounting", "accounts", "auditor", "audit"}, {"accounting", "accountancy", "finance", "audit", "tax"}),
        ({"nurse", "nursing", "doctor", "clinical", "caregiver"}, {"healthcare", "medical", "hospital", "clinical", "nursing", "nurse", "care"}),
        ({"developer", "software", "programmer", "devops", "cloud"}, {"technology", "digital", "software", "it", "engineering"}),
        ({"teacher", "lecturer", "academic", "trainer"}, {"education", "training", "academic", "teaching"}),
        ({"sales", "marketing", "brand"}, {"sales", "marketing", "retail", "business development"}),
        ({"chef", "hotel", "restaurant", "hospitality"}, {"hospitality", "tourism", "hotel", "food"}),
        ({"driver", "warehouse", "logistics", "procurement"}, {"transport", "logistics", "supply chain", "warehouse"}),
        ({"lawyer", "legal", "counsel"}, {"legal", "law"}),
        ({"hr", "recruiter", "recruitment"}, {"human resources", "recruitment", "administration"}),
    )
    for markers, category_terms in families:
        if words.intersection(markers):
            related.update(category_terms)
    return related


def _portal_search_terms(profile: CandidateProfile) -> list[str]:
    """Return exact and broadly useful role phrases for portal search forms."""
    phrases: list[str] = []
    if profile.target_position:
        phrases.append(profile.target_position.strip())
    phrases.extend(profile.likely_titles)
    modifiers = {
        "certified",
        "chartered",
        "experienced",
        "graduate",
        "junior",
        "lead",
        "licensed",
        "principal",
        "qualified",
        "registered",
        "senior",
        "staff",
    }
    for phrase in list(phrases):
        tokens = re.findall(r"[a-z0-9+#]+", phrase.lower())
        core = " ".join(token for token in tokens if token not in modifiers)
        if core and core != " ".join(tokens):
            phrases.append(core)
    return list(dict.fromkeys(phrase for phrase in phrases if phrase))[:6]


def _portal_navigation_priority(
    url: str,
    title: str,
    profile: CandidateProfile,
) -> tuple[int, str]:
    parsed = urlparse(url)
    text = unquote(f"{parsed.path} {parsed.query} {title}")
    normalized = re.sub(r"[^a-z0-9+#]+", " ", text.lower())
    if any(
        re.search(rf"\b{re.escape(term)}\b", normalized)
        for term in _portal_role_navigation_terms(profile)
    ):
        return (0, url)
    query_keys = {
        key.lower().rstrip("[]")
        for key in parse_qs(parsed.query, keep_blank_values=True)
    }
    if query_keys.intersection(
        {"q", "query", "s", "search", "keyword", "keywords", "search_keywords"}
    ):
        return (1, url)
    if _pagination_link(url, title):
        return (2, url)
    if _looks_like_job_category(title):
        return (3, url)
    return (4, url)


def _pagination_link(url: str, title: str) -> bool:
    parsed = urlparse(url)
    query_keys = {
        key.lower().rstrip("[]")
        for key in parse_qs(parsed.query, keep_blank_values=True)
    }
    if query_keys.intersection({"page", "paged", "pageno", "start", "offset"}):
        return True
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    if any(
        segment in {"page", "paged"}
        and index + 1 < len(segments)
        and segments[index + 1].isdigit()
        for index, segment in enumerate(segments)
    ):
        return True
    label = " ".join(title.lower().split()).strip()
    return label in {"next", "previous", "prev", "older", "newer", "›", "»", "→"} or bool(
        re.fullmatch(r"(?:page\s*)?\d+", label)
    )


def _looks_like_role_title(title: str) -> bool:
    lower = f" {title.lower()} "
    role_terms = (
        "engineer",
        "developer",
        "manager",
        "analyst",
        "officer",
        "executive",
        "accountant",
        "assistant",
        "specialist",
        "consultant",
        "architect",
        "designer",
        "coordinator",
        "administrator",
        "technician",
        "intern",
        "director",
        "scientist",
        "teacher",
        "lecturer",
        "nurse",
        "doctor",
        "cashier",
        "clerk",
        "supervisor",
        "mechanic",
        "electrician",
        "operator",
    )
    return any(re.search(rf"\b{re.escape(term)}s?\b", lower) for term in role_terms)


def _potential_job_detail_link(url: str, title: str) -> bool:
    if (
        not url
        or _is_homepage_url(url)
        or _listing_like_url(url)
        or _looks_like_listing_heading(title)
    ):
        return False
    parsed = urlparse(url)
    path_segments = {segment.lower() for segment in parsed.path.split("/") if segment}
    non_job_segments = {
        "account",
        "about",
        "auth",
        "blog",
        "contact",
        "employers",
        "faq",
        "help",
        "login",
        "news",
        "privacy",
        "register",
        "redirect",
        "resume",
        "share",
        "signin",
        "signup",
        "social",
        "terms",
        "training",
    }
    if path_segments.intersection(non_job_segments):
        return False
    suffix = parsed.path.rsplit("/", 1)[-1].lower()
    if "." in suffix and not suffix.endswith((".html", ".htm", ".php", ".aspx")):
        return False
    detail_query_keys = {
        "id",
        "job_id",
        "jobid",
        "jid",
        "vacancy_id",
        "vacancyid",
        "posting_id",
    }
    query_keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    return (
        _jobish_link(url, title)
        or _looks_like_role_title(title)
        or bool(query_keys.intersection(detail_query_keys))
    )


def _portal_search_form_urls(
    html: str,
    page_url: str,
    position: str,
) -> list[str]:
    """Build same-site GET search URLs from the portal's own forms."""
    if not position:
        return []
    urls: list[str] = []
    for match in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html, flags=re.I | re.S):
        attributes, body = match.groups()
        method = _html_attribute(attributes, "method").lower() or "get"
        if method != "get":
            continue
        action = urljoin(page_url, _html_attribute(attributes, "action") or page_url)
        if not _same_site(action, page_url):
            continue
        fields: list[tuple[int, str]] = []
        hidden: dict[str, str] = {}
        for input_match in re.finditer(r"<input\b([^>]*)>", body, flags=re.I | re.S):
            input_attributes = input_match.group(1)
            name = _html_attribute(input_attributes, "name").strip()
            if not name:
                continue
            field_type = _html_attribute(input_attributes, "type").lower()
            value = _html_attribute(input_attributes, "value")
            placeholder = _html_attribute(input_attributes, "placeholder").lower()
            lower_name = name.lower()
            if field_type == "hidden" and value:
                hidden[name] = value
                continue
            score = 0
            if field_type == "search":
                score += 4
            if lower_name in {
                "q",
                "s",
                "query",
                "search",
                "keyword",
                "keywords",
                "search_keywords",
                "search_query",
                "job_title",
                "position",
            }:
                score += 5
            if any(term in lower_name for term in ("search", "keyword", "query", "title")):
                score += 2
            if any(term in placeholder for term in ("job", "position", "keyword", "search")):
                score += 2
            if score:
                fields.append((score, name))
        if not fields:
            continue
        search_field = max(fields)[1]
        existing = {
            key: values[-1]
            for key, values in parse_qs(
                urlparse(action).query,
                keep_blank_values=True,
            ).items()
        }
        existing.update(hidden)
        existing[search_field] = position
        parsed_action = urlparse(action)
        search_url = _canonical_crawl_url(
            parsed_action._replace(query=urlencode(existing), fragment="").geturl()
        )
        if search_url and search_url not in urls:
            urls.append(search_url)
        if len(urls) >= 3:
            break
    return urls


def _crawl4ai_result_links(result: object, page_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    result_links = getattr(result, "links", None) or {}
    if isinstance(result_links, dict):
        for group in ("internal", "external"):
            items = result_links.get(group) or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                href = _canonical_crawl_url(
                    urljoin(page_url, str(item.get("href") or "").strip())
                )
                title = _clean_html(
                    str(item.get("text") or item.get("title") or item.get("aria_label") or "")
                )
                if href and title:
                    links.append((title, href))
    for title, href in _html_links(str(getattr(result, "html", "") or ""), page_url):
        canonical = _canonical_crawl_url(href)
        if canonical:
            links.append((title, canonical))
    deduped: dict[str, str] = {}
    for title, href in links:
        deduped.setdefault(href, title)
    return [(title, href) for href, title in deduped.items()]


def _schema_has_type(item: dict, wanted: str) -> bool:
    schema_type = item.get("@type")
    values = schema_type if isinstance(schema_type, list) else [schema_type]
    return any(str(value or "").lower() == wanted.lower() for value in values)


def _job_postings(html: str) -> list[dict]:
    return [item for item in _json_ld_items(html) if _schema_has_type(item, "JobPosting")]


def _structured_job_url(item: dict, base_url: str) -> str:
    value = item.get("url") or item.get("sameAs")
    if isinstance(value, dict):
        value = value.get("@id") or value.get("url")
    return _canonical_crawl_url(urljoin(base_url, str(value or ""))) if value else ""


def _collect_structured_job_links(
    html: str,
    page_url: str,
    origin: str,
    candidates: dict[str, tuple[str, str]],
    cap: int,
) -> None:
    for item in _job_postings(html):
        url = _structured_job_url(item, page_url)
        title = _clean_html(str(item.get("title") or item.get("name") or ""))
        if (
            url
            and _site_host(url) == origin
            and not _is_homepage_url(url)
            and not _listing_like_url(url)
        ):
            candidates.setdefault(url, (title or "View job", origin))
        if len(candidates) >= cap * 4:
            break


def _prioritize_job_candidates(
    candidates: dict[str, tuple[str, str]],
    profile: CandidateProfile,
) -> list[str]:
    def priority(item: tuple[str, tuple[str, str]]) -> tuple[int, int]:
        url, (title, _) = item
        text = unquote(f"{title} {url}").replace("-", " ").replace("_", " ")
        role_match = _portal_title_matches(profile, text)
        generic = title.lower().strip() in {"view job", "view details", "details", "apply"}
        return (0 if role_match else 1, 1 if generic else 0)

    return [url for url, _ in sorted(candidates.items(), key=priority)]


def _post_text(
    url: str,
    params: dict[str, str],
    headers: dict[str, str] | None = None,
    encoding: str = "utf-8",
    timeout: int = 30,
) -> str:
    request = Request(
        url,
        data=urlencode(params).encode("utf-8"),
        headers=headers or {"User-Agent": USER_AGENT, "Accept": "text/html"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode(encoding, errors="replace")


def _build_query(profile: CandidateProfile) -> str:
    terms = _query_terms(profile)
    return " ".join(terms[:6]) if terms else " ".join(profile.likely_titles[:2]) or "software"


def _search_queries(profile: CandidateProfile) -> list[str]:
    queries = []
    if profile.target_position:
        queries.append(profile.target_position)
        queries.extend(_position_variants(profile.target_position))
    title_queries = [title for title in profile.likely_titles if "engineer" in title or "scientist" in title]
    queries.extend(title_queries)
    if _has_ai_profile(profile) or _position_is_ai(profile.target_position):
        queries.extend(AI_ML_QUERY_TERMS)
    queries.append(_build_query(profile))
    return list(dict.fromkeys(query for query in queries if query))


def _query_terms(profile: CandidateProfile) -> list[str]:
    title_terms = sorted(
        [term for term in profile.likely_titles if len(term) > 2],
        key=lambda term: (
            not any(word in term for word in ("ai", "machine", "software", "data", "engineer", "developer")),
            -len(term),
        ),
    )
    priority = {skill: index for index, skill in enumerate(SEARCH_SKILL_PRIORITY)}
    skill_terms = sorted(
        [skill for skill in profile.skills if skill not in {"api", "git", "html", "css"}],
        key=lambda skill: (priority.get(skill, 999), skill),
    )
    return title_terms[:2] + skill_terms[:6]


def _remote_location_matches(location: str, country: str) -> bool:
    lower = location.lower()
    broad = ("worldwide", "anywhere", "global", "remote", "all regions")
    if any(term in lower for term in broad):
        return True
    if country == "sri lanka" and any(term in lower for term in ("asia", "apac", "oceania")):
        return True
    if country in {"germany", "france", "spain", "italy", "netherlands", "poland"} and "europe" in lower:
        return True
    return country in lower


def _matches_any_query(haystack: str, queries: list[str]) -> bool:
    if any(query in haystack for query in queries):
        return True
    ai_terms = ("machine learning", "artificial intelligence", " ai ", " ml ", "llm", "rag", "mlops", "nlp")
    technical_role_terms = ("engineer", "scientist", "developer", "architect")
    return any(term in f" {haystack} " for term in ai_terms) and any(term in haystack for term in technical_role_terms)


def _is_ai_ml_title(text: str, profile: CandidateProfile | None = None) -> bool:
    lower = f" {text.lower()} "
    if _looks_outside_sri_lanka(lower):
        return False
    if profile and profile.target_position:
        return _target_role_matches(lower, profile.target_position)
    positive = (
        "ai engineer",
        "ai/ml",
        "artificial intelligence",
        "computer vision",
        "data science",
        "data scientist",
        "deep learning",
        "generative ai",
        "llm",
        "machine learning",
        "ml engineer",
        "mlops",
        "nlp",
    )
    if any(term in lower for term in positive):
        return True
    return bool(re.search(r"\bai\b", lower) and re.search(r"\b(engineer|consultant|developer|architect|lead|intern)\b", lower))


ROLE_FAMILIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("devops", "site reliability", "platform engineer", "cloud engineer"),
        (
            "devops",
            "site reliability",
            "sre",
            "platform engineer",
            "platform engineering",
            "cloud engineer",
            "infrastructure engineer",
            "release engineer",
            "build engineer",
            "ci/cd engineer",
        ),
    ),
    (
        (
            "machine learning",
            "ml engineer",
            "artificial intelligence",
            "ai engineer",
            "data scientist",
        ),
        (
            "machine learning",
            "ml engineer",
            "mlops",
            "data scientist",
            "artificial intelligence",
            "ai engineer",
            "deep learning",
            "computer vision",
            "nlp engineer",
        ),
    ),
    (
        ("data engineer", "data analyst", "analytics engineer", "business intelligence"),
        (
            "data engineer",
            "data analyst",
            "data scientist",
            "analytics engineer",
            "business intelligence",
            "bi analyst",
            "etl developer",
        ),
    ),
    (
        (
            "software engineer",
            "software developer",
            "full stack developer",
            "backend developer",
            "frontend developer",
            "application engineer",
            "application developer",
        ),
        (
            "software engineer",
            "software developer",
            "full stack developer",
            "full-stack developer",
            "backend developer",
            "back-end developer",
            "frontend developer",
            "front-end developer",
            "application engineer",
            "application developer",
            "web developer",
            "mobile developer",
            "programmer",
        ),
    ),
    (
        ("quality assurance", "qa engineer", "test engineer", "software tester"),
        (
            "quality assurance",
            "qa engineer",
            "qa analyst",
            "software quality",
            "test engineer",
            "test automation",
            "automation engineer",
            "software tester",
            "sdet",
        ),
    ),
    (
        ("accountant", "accounting"),
        (
            "accountant",
            "accounting",
            "accounts executive",
            "accounts officer",
            "finance executive",
            "finance officer",
            "financial accountant",
            "management accountant",
            "auditor",
        ),
    ),
    (
        ("registered nurse", "nurse", "nursing"),
        ("registered nurse", "staff nurse", "nurse", "nursing"),
    ),
    (
        ("cybersecurity", "cyber security", "information security", "security analyst"),
        (
            "cybersecurity",
            "cyber security",
            "information security",
            "security analyst",
            "security engineer",
            "soc analyst",
            "penetration tester",
        ),
    ),
    (
        ("human resources", "hr executive", "hr manager", "recruiter"),
        (
            "human resources",
            "hr executive",
            "hr officer",
            "hr manager",
            "recruiter",
            "talent acquisition",
            "people operations",
        ),
    ),
    (
        ("digital marketing", "marketing executive", "marketing manager"),
        (
            "digital marketing",
            "marketing executive",
            "marketing manager",
            "brand executive",
            "social media",
            "seo specialist",
            "content marketing",
            "growth marketing",
        ),
    ),
)


def _normalized_role_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9+#]+", value.casefold()))


def _role_phrase_present(text: str, phrase: str) -> bool:
    normalized_text = f" {_normalized_role_text(text)} "
    normalized_phrase = _normalized_role_text(phrase)
    return bool(normalized_phrase and f" {normalized_phrase} " in normalized_text)


def _role_family(position: str) -> tuple[str, ...]:
    for markers, related in ROLE_FAMILIES:
        if any(_role_phrase_present(position, marker) for marker in markers):
            return related
    return ()


def _target_role_matches(text: str, target_position: str) -> bool:
    """Broad deterministic discovery gate; the final LLM remains the strict filter."""
    target = target_position.strip()
    if not target:
        return True
    if _role_phrase_present(text, target):
        return True
    family = _role_family(target)
    if family:
        return any(_role_phrase_present(text, related) for related in family)
    tokens = [
        token for token in re.findall(r"[a-z0-9+#]+", target.casefold())
        if len(token) > 1 and token not in {"junior", "senior", "lead"}
    ]
    generic_role_tokens = {
        "assistant", "associate", "consultant", "coordinator", "director",
        "engineer", "executive", "intern", "manager", "officer", "specialist",
        "supervisor", "technician", "trainee",
    }
    distinctive = [token for token in tokens if token not in generic_role_tokens]
    if distinctive:
        return any(_role_phrase_present(text, token) for token in distinctive)
    overlap = sum(_role_phrase_present(text, token) for token in tokens)
    return bool(tokens) and overlap >= max(1, (len(tokens) + 1) // 2)


def _looks_like_job_url_or_text(text: str, profile: CandidateProfile) -> bool:
    lower = text.lower()
    if not any(term in lower for term in ("job", "jobs", "career", "careers", "vacancy", "apply", "linkedin")):
        return False
    return _is_ai_ml_title(text, profile)


def _looks_outside_sri_lanka(text: str) -> bool:
    outside_terms = (
        "bangkok",
        "india",
        "uae",
        "dubai",
        "egypt",
        "us only",
        "usa only",
        "united states",
        "relocation provided",
    )
    return any(term in text for term in outside_terms)


def _position_variants(position: str) -> list[str]:
    lower = position.casefold()
    variants = list(_role_family(position))
    if "devops" in lower:
        variants.append("junior devops engineer")
    if "ai" in lower and "engineer" in lower:
        variants.extend(["artificial intelligence engineer", "generative ai engineer", "ai/ml engineer"])
    if "ml" in lower or "machine learning" in lower:
        variants.extend(["machine learning engineer", "ml engineer"])
    if "data scientist" in lower:
        variants.extend(["data scientist", "machine learning scientist"])
    normalized_position = _normalized_role_text(position)
    return [
        variant
        for variant in dict.fromkeys(variants)
        if _normalized_role_text(variant) != normalized_position
    ][:12]


def _position_is_ai(position: str) -> bool:
    lower = position.lower()
    return any(term in lower for term in ("ai", "artificial intelligence", "machine learning", "ml", "llm", "data scientist"))


def _clean_html(value: str) -> str:
    value = re_sub(r"<br\s*/?>", "\n", value)
    value = re_sub(r"</p>|</li>", "\n", value)
    value = re_sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return " ".join(value.split())


def re_sub(pattern: str, repl: str, value: str) -> str:
    import re

    return re.sub(pattern, repl, value, flags=re.I)


def _unix_to_iso(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _date_to_iso(value: object) -> str:
    if isinstance(value, int):
        return _unix_to_iso(value)
    return str(value or "")


def _salary(item: dict) -> str:
    low = item.get("salary_min")
    high = item.get("salary_max")
    if low and high:
        return f"{low} - {high}"
    if low:
        return str(low)
    if high:
        return str(high)
    return ""


def _himalayas_location(value: object) -> str:
    if not value:
        return "Worldwide"
    locations = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                locations.append(str(item.get("name") or item.get("slug") or item))
            else:
                locations.append(str(item))
    return ", ".join(locations) if locations else "Worldwide"


def _himalayas_salary(item: dict) -> str:
    low = item.get("minSalary")
    high = item.get("maxSalary")
    currency = item.get("currency") or ""
    period = item.get("salaryPeriod") or ""
    if low and high:
        return f"{currency} {low} - {high} {period}".strip()
    if low:
        return f"{currency} {low} {period}".strip()
    if high:
        return f"{currency} {high} {period}".strip()
    return ""


def _xml_text(item: ElementTree.Element, tag: str) -> str:
    child = item.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _split_at_title(title: str) -> tuple[str, str]:
    if " at " in title:
        role, company = title.rsplit(" at ", 1)
        return company.strip(), role.strip()
    return "", title.strip()


def _split_wwr_title(title: str) -> tuple[str, str]:
    if ":" in title:
        company, role = title.split(":", 1)
        return company.strip(), role.strip()
    return "", title.strip()


def _has_ai_profile(profile: CandidateProfile) -> bool:
    terms = {"ai/ml engineer", "ai engineer", "machine learning engineer"}
    skills = {"machine learning", "llm", "nlp", "rag", "mlops", "langchain"}
    return bool(terms.intersection(profile.likely_titles) or skills.intersection(profile.skills))


def _extract_location(text: str) -> str:
    match = re.search(r"Location:\s*(.*?)(?:\s+Job Type:|\s+Description:|\s+Company:|$)", text, flags=re.I)
    return match.group(1).strip() if match else ""


def _hidden_value(row: str, prefix: str) -> str:
    match = re.search(rf'id="{re.escape(prefix)}[^"]*"[^>]*>([^<]+)</span>', row, flags=re.I)
    return match.group(1).strip() if match else ""


def _row_number(row: str) -> str:
    match = re.search(r"openSizeWindow\('[^']*rid=(\d+)&", row, flags=re.I)
    return match.group(1) if match else "0"


def _topjobs_url(ac: str, ec: str, jc: str, rid: str) -> str:
    params = urlencode(
        {
            "ac": ac,
            "ec": ec,
            "index": rid,
            "jc": jc,
            "pg": "applicant/vacancyDetails.jsp",
            "rid": rid,
        }
    )
    return f"https://www.topjobs.lk/vacancy?{params}"


def _json_ld_items(html: str) -> list[dict]:
    """Return schema records from direct, @graph, and ItemList JSON-LD."""
    found: list[dict] = []

    def collect(payload: object) -> None:
        if isinstance(payload, list):
            for value in payload:
                collect(value)
            return
        if not isinstance(payload, dict):
            return
        graph = payload.get("@graph")
        if isinstance(graph, (dict, list)):
            collect(graph)
        elements = payload.get("itemListElement")
        if isinstance(elements, list):
            for element in elements:
                if isinstance(element, dict) and isinstance(element.get("item"), dict):
                    collect(element["item"])
                else:
                    collect(element)
        if _schema_has_type(payload, "ListItem") and isinstance(payload.get("item"), dict):
            collect(payload["item"])
        schema_type = payload.get("@type")
        if schema_type and not any(
            _schema_has_type(payload, container_type)
            for container_type in ("ItemList", "BreadcrumbList", "ListItem")
        ):
            found.append(payload)
        elif not schema_type and any(
            key in payload for key in ("url", "name", "title", "datePosted")
        ):
            found.append(payload)

    for match in re.finditer(
        r'<script\b[^>]*type=["\']application/ld\+json(?:;[^"\']*)?["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        raw = unescape(match.group(1)).strip()
        raw = re.sub(r"^\s*<!--|-->\s*$", "", raw)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        collect(payload)
    return found


def _select_job_posting(html: str, page_url: str) -> dict:
    postings = _job_postings(html)
    if not postings:
        return {}
    current = _clean_url(page_url)
    for item in postings:
        item_url = _structured_job_url(item, page_url)
        if item_url and _clean_url(item_url) == current:
            return item
    if len(postings) == 1 and not _listing_like_url(page_url):
        return postings[0]
    return {}


def _page_heading(html: str, fallback: str = "") -> str:
    patterns = (
        r"<h1\b[^>]*>(.*?)</h1>",
        r"<meta\b[^>]*property=[\"']og:title[\"'][^>]*>",
        r"<meta\b[^>]*name=[\"']twitter:title[\"'][^>]*>",
        r"<title\b[^>]*>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I | re.S)
        if not match:
            continue
        if match.lastindex:
            value = match.group(1)
        else:
            value = _html_attribute(match.group(0), "content")
        cleaned = _clean_html(value).strip()
        if cleaned:
            return cleaned
    return _clean_html(fallback).strip()


def _rendered_page_description(html: str, rendered_text: str = "") -> str:
    cleaned_html = re.sub(
        r"<(script|style|noscript|svg|nav|header|footer)\b.*?</\1>",
        " ",
        html,
        flags=re.I | re.S,
    )
    candidates: list[str] = []
    semantic_patterns = (
        r"<main\b[^>]*>(.*?)</main>",
        r"<article\b[^>]*>(.*?)</article>",
        r"<(?:div|section)\b[^>]*(?:id|class)=[\"'][^\"']*(?:job[-_\s]?description|job[-_\s]?details|vacancy[-_\s]?details|description|posting[-_\s]?content)[^\"']*[\"'][^>]*>(.*?)</(?:div|section)>",
    )
    for pattern in semantic_patterns:
        for match in re.finditer(pattern, cleaned_html, flags=re.I | re.S):
            value = _clean_html(match.group(1))
            if value:
                candidates.append(value)
    rendered = _clean_html(rendered_text)
    if rendered:
        candidates.append(rendered)
    body_match = re.search(r"<body\b[^>]*>(.*?)</body>", cleaned_html, flags=re.I | re.S)
    body = _clean_html(body_match.group(1) if body_match else cleaned_html)
    if body:
        candidates.append(body)
    if not candidates:
        return ""
    return max(candidates, key=len)[:12000]


def _html_job_detail_evidence(html: str, description: str) -> bool:
    if len(description) < 60:
        return False
    lower = f" {_clean_html(html)} {description} ".lower()
    signals = (
        "apply now",
        "apply for",
        "how to apply",
        "job description",
        "responsibilities",
        "requirements",
        "qualifications",
        "employment type",
        "closing date",
        "date posted",
        "valid through",
        "experience required",
        "key duties",
    )
    return any(signal in lower for signal in signals)


def _structured_company(item: dict, url: str, title: str) -> str:
    organization = item.get("hiringOrganization") or {}
    if isinstance(organization, list):
        organization = next(
            (value for value in organization if isinstance(value, dict)),
            {},
        )
    if isinstance(organization, dict):
        name = _clean_html(str(organization.get("name") or ""))
        if name:
            return name
    return _company_from_url_or_title(url, title)


def _structured_location(item: dict, country: str) -> str:
    locations = item.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    values: list[str] = []
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address") or {}
            if isinstance(address, str):
                values.append(_clean_html(address))
                continue
            if not isinstance(address, dict):
                address = {}
            parts = [
                str(address.get(key) or "")
                for key in (
                    "addressLocality",
                    "addressRegion",
                    "addressCountry",
                )
            ]
            value = ", ".join(part for part in parts if part)
            if value:
                values.append(value)
    if str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        values.append("Remote")
    return ", ".join(dict.fromkeys(values)) or country.title()


def _structured_job_is_expired(item: dict) -> bool:
    value = str(item.get("validThrough") or "").strip()
    if not value:
        return False
    try:
        normalized = value.replace("Z", "+00:00")
        valid_through = datetime.fromisoformat(normalized)
        if "T" not in normalized:
            valid_through = valid_through.replace(hour=23, minute=59, second=59)
        if valid_through.tzinfo is None:
            valid_through = valid_through.replace(tzinfo=timezone.utc)
        return valid_through.astimezone(timezone.utc) < datetime.now(timezone.utc)
    except ValueError:
        return False


def _portal_job_from_page(
    source: str,
    url: str,
    html: str,
    fallback_title: str,
    profile: CandidateProfile,
    country: str,
    rendered_text: str = "",
) -> Job | None:
    """Validate a fetched detail page and convert it to an evidence-backed job."""
    if not html or _is_homepage_url(url) or _listing_like_url(url):
        return None
    item = _select_job_posting(html, url)
    if item and _structured_job_is_expired(item):
        return None
    title = _clean_html(
        str(item.get("title") or item.get("name") or "")
        if item
        else _page_heading(html, fallback_title)
    ).strip()
    if not title or _looks_like_listing_heading(title):
        return None
    if not _portal_title_matches(profile, title):
        return None
    description = _clean_html(str(item.get("description") or "")) if item else ""
    if not description:
        description = _rendered_page_description(html, rendered_text)
    if not item and not _html_job_detail_evidence(html, description):
        return None
    normalized_country = normalize_country(country)
    return Job(
        source=source,
        source_id=_clean_url(url),
        title=title,
        company=_structured_company(item, url, title),
        location=_structured_location(item, normalized_country),
        country_hint=normalized_country,
        url=url,
        description=description or title,
        published_at=str(item.get("datePosted") or ""),
        salary="",
        job_type=str(item.get("employmentType") or ""),
        detail_page_verified=True,
    )


def _title_from_slug(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def _first_group(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.I | re.S)
    return match.group(1).strip() if match else ""


def _duckduckgo_results(html: str) -> list[dict[str, str]]:
    results = []
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|<div[^>]+class="result__snippet"[^>]*>(.*?)</div>)',
        html,
        flags=re.I | re.S,
    ):
        href = unescape(match.group(1))
        title = _clean_html(match.group(2))
        snippet = _clean_html(match.group(3) or match.group(4) or "")
        results.append({"url": _duckduckgo_url(href), "title": title, "snippet": snippet})
    return results


def _duckduckgo_url(href: str) -> str:
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(uddg) if uddg else href
    return href


def _company_from_url_or_title(url: str, title: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if " at " in title.lower():
        return title.rsplit(" at ", 1)[-1].strip()
    return host


def _search_result_job(source: str, title: str, url: str, snippet: str, country: str) -> Job:
    canonical_url = _canonical_crawl_url(url) or url
    return Job(
        source=source,
        source_id=canonical_url.lower().strip(),
        title=title,
        company=_company_from_url_or_title(canonical_url, title),
        location=country.title(),
        country_hint=country,
        url=canonical_url,
        description=snippet,
        published_at="",
        salary="",
        job_type="search result",
    )


def _bounded_env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _search_discovery_query_cap() -> int:
    """Bound paid/public search calls even when a very large source limit is used."""
    return _bounded_env_int(
        "WEB_DISCOVERY_MAX_QUERIES_PER_SOURCE",
        4,
        minimum=1,
        maximum=20,
    )


def _search_discovery_candidate_cap(limit: int) -> int:
    configured = _bounded_env_int(
        "WEB_DISCOVERY_MAX_DETAIL_PAGES_PER_SOURCE",
        30,
        minimum=1,
        maximum=100,
    )
    # A small over-fetch lets validation discard expired, listing, and unrelated
    # results without turning a requested result limit into unbounded crawling.
    return min(configured, max(8, max(1, limit) * 3))


def _search_discovery_worker_cap(candidate_count: int) -> int:
    configured = _bounded_env_int(
        "WEB_DISCOVERY_DETAIL_WORKERS",
        6,
        minimum=1,
        maximum=12,
    )
    return max(1, min(configured, candidate_count))


def _validate_search_discovery_candidates(
    candidates: list[Job],
    profile: CandidateProfile,
    country: str,
    limit: int,
) -> list[Job]:
    """Turn bounded search snippets into fetched, evidence-backed job records."""
    if limit <= 0:
        return []
    candidate_cap = _search_discovery_candidate_cap(limit)
    canonical_candidates: list[Job] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = _canonical_crawl_url(candidate.url)
        key = url.lower().strip()
        if (
            not url
            or key in seen
            or _is_homepage_url(url)
            or _listing_like_url(url)
        ):
            continue
        seen.add(key)
        canonical_candidates.append(replace(candidate, url=url, source_id=key))
        if len(canonical_candidates) >= candidate_cap:
            break
    if not canonical_candidates:
        return []

    validated_by_index: dict[int, Job] = {}
    worker_count = _search_discovery_worker_cap(len(canonical_candidates))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _fetch_search_discovery_detail_job,
                candidate,
                profile,
                country,
            ): index
            for index, candidate in enumerate(canonical_candidates)
        }
        for future in as_completed(futures):
            index = futures[future]
            candidate = canonical_candidates[index]
            try:
                job = future.result()
            except Exception as exc:
                logger.warning(
                    "%s detail validation failed: %s: %s",
                    candidate.source,
                    candidate.url,
                    exc,
                )
                continue
            if job is not None:
                validated_by_index[index] = job

    validated = [
        validated_by_index[index]
        for index in range(len(canonical_candidates))
        if index in validated_by_index
    ]
    return _dedupe_jobs(validated)[:limit]


def _fetch_search_discovery_detail_job(
    candidate: Job,
    profile: CandidateProfile,
    country: str,
) -> Job | None:
    html = _get_text(candidate.url, headers=BROWSER_HEADERS, timeout=15)
    if _job_page_explicitly_closed(html):
        return None
    job = _portal_job_from_page(
        source=candidate.source,
        url=candidate.url,
        html=html,
        fallback_title=candidate.title,
        profile=profile,
        country=country,
    )
    if job is None:
        return None

    item = _select_job_posting(html, candidate.url)
    evidence_location = _search_detail_location(item, html)
    country_label = _country_evidence_label(
        f"{evidence_location} {_clean_html(html)}",
        country,
    )
    if (
        not country_label
        and normalize_country(country) == "sri lanka"
        and re.search(r"(?<![a-z])lk(?![a-z])", evidence_location.lower())
    ):
        country_label = "Sri Lanka"
    if not country_label:
        return None
    published_at = job.published_at or _search_detail_published_at(html)
    return replace(
        job,
        source_id=candidate.url.lower().strip(),
        location=evidence_location or country_label,
        country_hint=normalize_country(country),
        published_at=published_at,
        detail_page_verified=True,
    )


def _job_page_explicitly_closed(html: str) -> bool:
    visible_html = re.sub(
        r"<(?:script|style|noscript)\b.*?</(?:script|style|noscript)>",
        " ",
        html,
        flags=re.I | re.S,
    )
    text = " ".join(_clean_html(visible_html).lower().split())
    phrases = (
        "applications are closed",
        "application deadline has passed",
        "job has expired",
        "job is expired",
        "job is no longer available",
        "no longer accepting applications",
        "position has been filled",
        "position is filled",
        "this vacancy has closed",
        "vacancy has expired",
    )
    return any(phrase in text for phrase in phrases)


def _search_detail_location(item: dict, html: str) -> str:
    if item and (
        item.get("jobLocation")
        or str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    ):
        return _structured_location(item, "")

    for match in re.finditer(r"<meta\b([^>]*)>", html, flags=re.I | re.S):
        attributes = match.group(1)
        field = " ".join(
            (
                _html_attribute(attributes, "name"),
                _html_attribute(attributes, "property"),
                _html_attribute(attributes, "itemprop"),
            )
        ).lower()
        if any(
            marker in field
            for marker in ("joblocation", "job_location", "geo.placename", "location")
        ):
            value = _clean_html(_html_attribute(attributes, "content"))
            if value:
                return value

    visible = re.sub(
        r"</(?:address|dd|div|li|p|section|span|td|tr)>|<br\s*/?>",
        "\n",
        html,
        flags=re.I,
    )
    visible = re.sub(r"<[^>]+>", " ", visible)
    lines = [
        " ".join(unescape(line).split()).strip()
        for line in visible.splitlines()
    ]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        match = re.match(
            r"^(?:job\s+)?location\s*(?::|-)\s*(.{2,100})$",
            line,
            flags=re.I,
        )
        if match:
            return match.group(1).strip(" |,")
        if (
            re.fullmatch(r"(?:job\s+)?location\s*(?::|-)?", line, flags=re.I)
            and index + 1 < len(lines)
        ):
            return lines[index + 1][:100].strip(" |,")
    return ""


def _search_detail_published_at(html: str) -> str:
    for match in re.finditer(r"<meta\b([^>]*)>", html, flags=re.I | re.S):
        attributes = match.group(1)
        field = " ".join(
            (
                _html_attribute(attributes, "name"),
                _html_attribute(attributes, "property"),
                _html_attribute(attributes, "itemprop"),
            )
        ).lower()
        if any(
            marker in field
            for marker in (
                "dateposted",
                "datepublished",
                "publishdate",
                "article:published_time",
            )
        ):
            value = _clean_html(_html_attribute(attributes, "content"))
            if value:
                return value
    for match in re.finditer(r"<time\b([^>]*)>", html, flags=re.I | re.S):
        attributes = match.group(1)
        context = " ".join(
            (
                attributes,
                _html_attribute(attributes, "itemprop"),
                _html_attribute(attributes, "class"),
            )
        ).lower()
        value = _html_attribute(attributes, "datetime")
        if value and any(term in context for term in ("dateposted", "post", "publish")):
            return value
    return ""


def _country_evidence_label(text: str, country: str) -> str:
    normalized = normalize_country(country)
    if not normalized:
        return ""
    lower = f" {text.lower()} "
    if normalized == "sri lanka":
        aliases = (
            ("sri lanka", "Sri Lanka"),
            ("colombo", "Colombo"),
            ("kandy", "Kandy"),
            ("galle", "Galle"),
            ("jaffna", "Jaffna"),
            ("negombo", "Negombo"),
            ("kurunegala", "Kurunegala"),
            ("gampaha", "Gampaha"),
            ("kalutara", "Kalutara"),
            ("matara", "Matara"),
            ("ratnapura", "Ratnapura"),
            ("batticaloa", "Batticaloa"),
            ("anuradhapura", "Anuradhapura"),
            ("sri jayawardenepura", "Sri Jayawardenepura"),
        )
    else:
        aliases = ((normalized, normalized.title()),)
    for alias, label in aliases:
        if re.search(
            rf"(?<![a-z]){re.escape(alias)}(?![a-z])",
            lower,
        ):
            return label
    return ""


def _dedupe_jobs(jobs: Iterable[Job]) -> list[Job]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[Job] = []
    for job in jobs:
        key = (
            job.source.lower().strip(),
            job.source_id.lower().strip() or _clean_url(job.url),
            job.title.lower().strip(),
            job.company.lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def _clean_url(url: str) -> str:
    return url.split("?", 1)[0].lower().strip()
