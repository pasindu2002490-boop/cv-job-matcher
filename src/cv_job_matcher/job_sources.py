from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from html import unescape
from typing import Iterable
from xml.etree import ElementTree
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from .country import adzuna_country_code, normalize_country
from .models import CandidateProfile, Job


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
    endpoint = "https://itpro.lk/rss/ai-and-data/"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        root = _get_xml(self.endpoint)
        jobs = []
        for item in root.findall("./channel/item"):
            title = _xml_text(item, "title")
            if not _is_ai_ml_title(title, profile):
                continue
            company, role = _split_at_title(title)
            description = _clean_html(
                " ".join(
                    [
                        _xml_text(item, "description"),
                        _xml_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded"),
                    ]
                )
            )
            jobs.append(
                Job(
                    source=self.name,
                    source_id=_xml_text(item, "guid") or _xml_text(item, "link"),
                    title=role,
                    company=company,
                    location=_extract_location(description) or "Sri Lanka",
                    country_hint="sri lanka",
                    url=_xml_text(item, "link"),
                    description=description,
                    published_at=_xml_text(item, "pubDate"),
                    salary="",
                    job_type="",
                )
            )
        return _dedupe_jobs(jobs)[:limit]


class TopJobsSriLankaProvider(JobProvider):
    name = "topjobs.lk"
    endpoints = (
        "https://www.topjobs.lk/applicant/vacancybyfunctionalarea.jsp?FA=SDQ",
        "https://www.topjobs.lk/applicant/vacancybyfunctionalarea.jsp?FA=HNS",
    )

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        jobs = []
        for endpoint in self.endpoints:
            html = _get_text(endpoint, headers=BROWSER_HEADERS, encoding="iso-8859-1")
            for match in re.finditer(r"<tr\b.*?</tr>", html, flags=re.I | re.S):
                row = match.group(0)
                text = _clean_html(row)
                if not _is_ai_ml_title(text, profile):
                    continue
                title_match = re.search(r"<h2><span>(.*?)</span></h2>", row, flags=re.I | re.S)
                company_match = re.search(r"<h1>(.*?)</h1>", row, flags=re.I | re.S)
                if not title_match:
                    continue
                title = _clean_html(title_match.group(1))
                if not _is_ai_ml_title(title, profile):
                    continue
                company = _clean_html(company_match.group(1)) if company_match else ""
                jc = _hidden_value(row, "hdnJC")
                ec = _hidden_value(row, "hdnEC") or "DEFZZZ"
                ac = _hidden_value(row, "hdnAC") or "DEFZZZ"
                rid = _row_number(row)
                url = _topjobs_url(ac, ec, jc, rid) if jc else endpoint
                cells = [_clean_html(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.I | re.S)]
                published = cells[-3] if len(cells) >= 3 else ""
                closing = cells[-2] if len(cells) >= 2 else ""
                location = cells[-1] if cells else "Sri Lanka"
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=jc or url,
                        title=title,
                        company=company,
                        location=location or "Sri Lanka",
                        country_hint="sri lanka",
                        url=url,
                        description=f"{text} Closing date: {closing}",
                        published_at=published,
                        salary="",
                        job_type="",
                    )
                )
        return _dedupe_jobs(jobs)[:limit]


class XpressJobsSriLankaProvider(JobProvider):
    name = "XpressJobs"
    endpoint = "https://xpress.jobs/api/jobs/searchJobs"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        jobs = []
        per_query_limit = max(1, limit // max(1, len(_search_queries(profile))))
        for query in _search_queries(profile):
            try:
                payload = _get_json_list(self.endpoint, {"KeyWord": query, "page": "1"})
            except (HTTPError, URLError, TimeoutError, OSError):
                if jobs:
                    return _dedupe_jobs(jobs)[:limit]
                raise
            for item in payload:
                title = str(item.get("jobTitle") or "")
                haystack = f"{title} {item.get('overview') or ''}"
                if not _is_ai_ml_title(haystack, profile):
                    continue
                job_id = str(item.get("jobId") or "")
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=job_id,
                        title=title,
                        company=str(item.get("organizationName") or ""),
                        location=str(item.get("locations") or "Sri Lanka").strip(),
                        country_hint="sri lanka",
                        url=f"https://xpress.jobs/jobs/view/{job_id}" if job_id else "https://xpress.jobs/jobs",
                        description=_clean_html(str(item.get("overview") or "")),
                        published_at="",
                        salary="",
                        job_type=str(item.get("jobType") or ""),
                    )
                )
                if len(jobs) >= per_query_limit * len(_search_queries(profile)):
                    break
        return _dedupe_jobs(jobs)[:limit]


class RemoteRocketshipProvider(JobProvider):
    name = "RemoteRocketship"
    is_remote_global = True
    endpoints = (
        "https://www.remoterocketship.com/country/sri-lanka/jobs/machine-learning-engineer/",
        "https://www.remoterocketship.com/country/sri-lanka/jobs/ai-engineer/",
        "https://www.remoterocketship.com/country/sri-lanka/jobs/data-scientist/",
    )

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if normalize_country(country) != "sri lanka":
            return []
        jobs = []
        for endpoint in self.endpoints:
            html = _get_text(endpoint, headers=BROWSER_HEADERS)
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
            for start in (0,):
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
        return _dedupe_jobs(jobs)[:limit]

    def _endpoints(self, profile: CandidateProfile) -> list[str]:
        queries = list(self.base_queries)
        queries.extend(_search_queries(profile))
        endpoints = []
        for query in dict.fromkeys(q for q in queries if q):
            slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
            if slug:
                endpoints.append(f"https://lk.linkedin.com/jobs/{slug}-jobs")
        endpoints.extend(
            [
                "https://lk.linkedin.com/jobs/artificial-intelligence-ai-jobs",
            ]
        )
        return list(dict.fromkeys(endpoints))


class DuckDuckGoDiscoveryProvider(JobProvider):
    name = "DuckDuckGo Discovery"
    endpoint = "https://html.duckduckgo.com/html/"
    is_search_discovery = True

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        queries = self._queries(profile, country)
        jobs = []
        for query in queries:
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
                if jobs:
                    return _dedupe_jobs(jobs)[:limit]
                raise
            for item in _duckduckgo_results(html):
                title = item["title"]
                url = item["url"]
                snippet = item["snippet"]
                if not url or not _looks_like_job_url_or_text(f"{title} {snippet} {url}", profile):
                    continue
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=_clean_url(url),
                        title=title,
                        company=_company_from_url_or_title(url, title),
                        location=country.title(),
                        country_hint=country,
                        url=url,
                        description=snippet,
                        published_at="",
                        salary="",
                        job_type="search result",
                    )
                )
                if len(jobs) >= limit:
                    return _dedupe_jobs(jobs)
        return _dedupe_jobs(jobs)[:limit]

    def _queries(self, profile: CandidateProfile, country: str) -> list[str]:
        position = profile.target_position or "AI Engineer"
        base = [
            f'"{position}" "{country}" apply job',
            f'"{position}" "{country}" vacancy',
            f'"AI ML Engineer" "{country}" apply',
            f'"Machine Learning Engineer" "{country}" apply',
            f'"Generative AI Engineer" "{country}" apply',
        ]
        if country == "sri lanka":
            for domain in SRI_LANKA_SEARCH_DOMAINS:
                base.append(f'site:{domain} "{position}" "Sri Lanka"')
                base.append(f'site:{domain} "AI Engineer" OR "AI/ML Engineer"')
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
        if self.disabled_reason:
            return []
        api_key = os.getenv("GOOGLE_CSE_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        cx = os.getenv("GOOGLE_CSE_ID", "").strip()
        jobs = []
        for query in self._queries(profile, country):
            payload = _get_json(self.endpoint, {"key": api_key, "cx": cx, "q": query, "num": "10"})
            for item in payload.get("items", []):
                title = str(item.get("title") or "")
                url = str(item.get("link") or "")
                snippet = str(item.get("snippet") or "")
                if not url or not _looks_like_job_url_or_text(f"{title} {snippet} {url}", profile):
                    continue
                jobs.append(_search_result_job(self.name, title, url, snippet, country))
                if len(jobs) >= limit:
                    return _dedupe_jobs(jobs)
        return _dedupe_jobs(jobs)[:limit]


class SerpApiGoogleProvider(DuckDuckGoDiscoveryProvider):
    name = "SerpAPI Google"
    endpoint = "https://serpapi.com/search.json"
    is_search_discovery = True

    @property
    def disabled_reason(self) -> str:
        return "" if os.getenv("SERPAPI_API_KEY", "").strip() else "set SERPAPI_API_KEY"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        if self.disabled_reason:
            return []
        api_key = os.getenv("SERPAPI_API_KEY", "").strip()
        jobs = []
        for query in self._queries(profile, country):
            payload = _get_json(
                self.endpoint,
                {"engine": "google", "api_key": api_key, "q": query, "location": country.title(), "num": "20"},
            )
            for item in payload.get("organic_results", []):
                title = str(item.get("title") or "")
                url = str(item.get("link") or "")
                snippet = str(item.get("snippet") or "")
                if not url or not _looks_like_job_url_or_text(f"{title} {snippet} {url}", profile):
                    continue
                jobs.append(_search_result_job(self.name, title, url, snippet, country))
                if len(jobs) >= limit:
                    return _dedupe_jobs(jobs)
        return _dedupe_jobs(jobs)[:limit]


class Crawl4AiSeedProvider(JobProvider):
    name = "Crawl4AI Seeds"
    is_search_discovery = True

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
        # This provider is intentionally conservative: it only records configured seed URLs
        # as discovered targets. Full async crawl extraction should be implemented per site
        # after respecting each site's terms and robots behavior.
        jobs = []
        for url in os.getenv("CRAWL4AI_SEED_URLS", "").split(","):
            clean = url.strip()
            if not clean:
                continue
            title = _title_from_slug(clean) or profile.target_position or "Job discovery seed"
            jobs.append(_search_result_job(self.name, title, clean, "Crawl4AI seed URL", country))
            if len(jobs) >= limit:
                break
        return jobs


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
    return [
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
        if getattr(provider, "is_search_discovery", False) and not web_discovery:
            notes.append(f"{provider.name}: skipped (use --web-discovery to include search-engine discovery)")
            continue
        if provider.disabled_reason:
            notes.append(f"{provider.name}: skipped ({provider.disabled_reason})")
            continue
        if provider.is_remote_global and not include_remote_global:
            notes.append(f"{provider.name}: skipped (use --include-remote-global to include worldwide remote boards)")
            continue
        try:
            result = provider.search(profile, country, limit_per_source)
            jobs.extend(result)
            status = f"{provider.name}: {len(result)} jobs"
            if isinstance(provider, AdzunaProvider) and not provider.enabled:
                status += " (disabled: set ADZUNA_APP_ID and ADZUNA_APP_KEY for broader country coverage)"
            notes.append(status)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            notes.append(f"{provider.name}: failed: {exc}")
    return _dedupe_jobs(jobs), notes


def _get_json(url: str, params: dict[str, str] | None = None) -> dict:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _get_json_list(url: str, params: dict[str, str] | None = None) -> list:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, list) else []


def _get_xml(url: str) -> ElementTree.Element:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, text/xml"})
    with urlopen(request, timeout=30) as response:
        return ElementTree.fromstring(response.read())


def _get_text(
    url: str,
    headers: dict[str, str] | None = None,
    encoding: str = "utf-8",
    timeout: int = 30,
) -> str:
    request = Request(url, headers=headers or {"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode(encoding, errors="replace")


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
        target = profile.target_position.lower().strip()
        if target and target in lower:
            return True
        target_tokens = [token for token in re.findall(r"[a-z0-9+#]+", target) if len(token) > 1]
        if target_tokens and all(token in lower for token in target_tokens):
            return True
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
    lower = position.lower()
    variants = []
    if "ai" in lower and "engineer" in lower:
        variants.extend(["artificial intelligence engineer", "generative ai engineer", "ai/ml engineer"])
    if "ml" in lower or "machine learning" in lower:
        variants.extend(["machine learning engineer", "ml engineer"])
    if "data scientist" in lower:
        variants.extend(["data scientist", "machine learning scientist"])
    return variants


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
    found: list[dict] = []
    for match in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, flags=re.I | re.S):
        try:
            payload = json.loads(unescape(match.group(1)))
        except json.JSONDecodeError:
            continue
        elements = payload.get("itemListElement") if isinstance(payload, dict) else None
        if isinstance(elements, list):
            for element in elements:
                if isinstance(element, dict):
                    item = element.get("item") if isinstance(element.get("item"), dict) else element
                    if isinstance(item, dict):
                        found.append(item)
    return found


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
    return Job(
        source=source,
        source_id=_clean_url(url),
        title=title,
        company=_company_from_url_or_title(url, title),
        location=country.title(),
        country_hint=country,
        url=url,
        description=snippet,
        published_at="",
        salary="",
        job_type="search result",
    )


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
