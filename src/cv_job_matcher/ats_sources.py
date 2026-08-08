from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .job_sources import JobProvider, job_title_matches_profile
from .models import CandidateProfile, Job


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cv-job-matcher/0.1)",
    "Accept": "application/json,application/rss+xml,text/xml,text/html",
}


class SuccessFactorsRssProvider(JobProvider):
    def __init__(self, name: str, rss_url: str, company: str) -> None:
        self.name = name
        self.rss_url = rss_url
        self.company = company

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        root = ElementTree.fromstring(_get(self.rss_url))
        items = root.findall("./channel/item")
        self.last_inventory_count = len(items)
        jobs: list[Job] = []
        for item in items:
            raw_title = (item.findtext("title") or "").strip()
            title = re.sub(r"\s*\([^()]*(?:LK|Sri Lanka)[^()]*\)\s*$", "", raw_title).strip()
            if not job_title_matches_profile(profile, title):
                continue
            link = (item.findtext("link") or "").strip()
            description = _clean_html(item.findtext("description") or "")
            location_match = re.search(r"\(([^()]*(?:LK|Sri Lanka)[^()]*)\)\s*$", raw_title, re.I)
            location = location_match.group(1) if location_match else "Sri Lanka"
            jobs.append(Job(
                source=self.name,
                source_id=(item.findtext("guid") or link).strip(),
                title=title,
                company=self.company,
                location=location,
                country_hint="sri lanka",
                url=link,
                description=description,
                published_at=(item.findtext("pubDate") or "").strip(),
                detail_page_verified=True,
            ))
            if len(jobs) >= limit:
                break
        return jobs


class SmartRecruitersProvider(JobProvider):
    def __init__(self, name: str, company_id: str, company: str) -> None:
        self.name = name
        self.company_id = company_id
        self.company = company

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        endpoint = f"https://api.smartrecruiters.com/v1/companies/{self.company_id}/postings?limit=100"
        payload = json.loads(_get(endpoint))
        sri_lanka_rows = [
            row for row in payload.get("content", [])
            if str((row.get("location") or {}).get("country") or "").casefold() in {"lk", "lka"}
            or "sri lanka" in str((row.get("location") or {}).get("fullLocation") or "").casefold()
        ]
        self.last_inventory_count = len(sri_lanka_rows)
        jobs: list[Job] = []
        for row in sri_lanka_rows:
            location = row.get("location") or {}
            full_location = str(location.get("fullLocation") or "")
            title = str(row.get("name") or "").strip()
            if not job_title_matches_profile(profile, title):
                continue
            job_id = str(row.get("id") or "")
            detail = json.loads(_get(str(row.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{self.company_id}/postings/{job_id}")))
            sections = (detail.get("jobAd") or {}).get("sections") or {}
            description = " ".join(
                _clean_html(str(section.get("text") or ""))
                for section in sections.values()
                if isinstance(section, dict)
            ).strip()
            jobs.append(Job(
                source=self.name,
                source_id=job_id,
                title=title,
                company=self.company,
                location=full_location or "Sri Lanka",
                country_hint="sri lanka",
                url=f"https://jobs.smartrecruiters.com/{self.company_id}/{job_id}",
                description=description,
                published_at=str(row.get("releasedDate") or ""),
                job_type=str((row.get("typeOfEmployment") or {}).get("label") or ""),
                detail_page_verified=True,
            ))
            if len(jobs) >= limit:
                break
        return jobs


class ManatalCareerProvider(JobProvider):
    def __init__(self, name: str, slug: str, company: str) -> None:
        self.name = name
        self.slug = slug
        self.company = company

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        endpoint = f"https://www.careers-page.com/api/v1.0/c/{self.slug}/jobs/?page_size=100&page=1"
        rows = [
            row for row in json.loads(_get(endpoint)).get("results", [])
            if str(row.get("country") or "").casefold() == "sri lanka"
        ]
        self.last_inventory_count = len(rows)
        jobs: list[Job] = []
        for row in rows:
            title = str(row.get("position_name") or "").strip()
            if not job_title_matches_profile(profile, title):
                continue
            job_hash = str(row.get("hash") or "")
            jobs.append(Job(
                source=self.name,
                source_id=str(row.get("id") or job_hash),
                title=title,
                company=self.company,
                location=str(row.get("location_display") or "Sri Lanka"),
                country_hint="sri lanka",
                url=f"https://www.careers-page.com/{self.slug}/job/{job_hash}",
                description=_clean_html(str(row.get("description") or "")),
                detail_page_verified=True,
            ))
            if len(jobs) >= limit:
                break
        return jobs


class TrakstarCareerProvider(JobProvider):
    def __init__(self, name: str, board_url: str, company: str) -> None:
        self.name = name
        self.board_url = board_url
        self.company = company

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        listing_html = _get(self.board_url)
        jobs: list[Job] = []
        seen: set[str] = set()
        listing_links = re.findall(
            r'''href=["']([^"']*/jobs/[^"']+/)["'][^>]*>(.*?)</a>''',
            listing_html,
            re.I | re.S,
        )
        self.last_inventory_count = len({urljoin(self.board_url, unescape(href)) for href, _ in listing_links})
        for href, body in listing_links:
            url = urljoin(self.board_url, unescape(href))
            if url in seen:
                continue
            seen.add(url)
            title = _clean_html(body)
            if not title or not job_title_matches_profile(profile, title):
                continue
            detail_html = _get(url)
            detail_text = _clean_html(detail_html)
            if "sri lanka" not in detail_text.casefold() and "colombo" not in detail_text.casefold():
                continue
            jobs.append(Job(
                source=self.name,
                source_id=url.rstrip("/").rsplit("/", 1)[-1],
                title=title,
                company=self.company,
                location="Colombo, Sri Lanka",
                country_hint="sri lanka",
                url=url,
                description=detail_text,
                detail_page_verified=True,
            ))
            if len(jobs) >= limit:
                break
        return jobs


class RootcodeApiProvider(JobProvider):
    """Complete Sri Lankan inventory from Rootcode's public careers JSON API."""

    def __init__(self, name: str = "Rootcode Careers") -> None:
        self.name = name

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        rows = [
            row for row in json.loads(_get("https://rootcode.ai/api/jobs")).get("results", [])
            if str(row.get("country") or "").casefold() == "sri lanka"
        ]
        self.last_inventory_count = len(rows)
        jobs: list[Job] = []
        for row in rows:
            title = str(row.get("position_name") or "").strip()
            if not job_title_matches_profile(profile, title):
                continue
            job_id = str(row.get("id") or "")
            slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
            jobs.append(Job(
                source=self.name,
                source_id=job_id,
                title=title,
                company="Rootcode",
                location=str(row.get("location_display") or "Colombo, Sri Lanka"),
                country_hint="sri lanka",
                url=f"https://rootcode.ai/careers/{slug}-{job_id}",
                description=_clean_html(str(row.get("description") or "")),
                detail_page_verified=True,
            ))
            if len(jobs) >= limit:
                break
        return jobs


class IfsCareerProvider(JobProvider):
    """Complete IFS inventory exposed by the official careers page."""

    name = "IFS Sri Lanka Careers"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        payload = json.loads(_get("https://www.ifs.com/api/smartrecruiters?q=&location=&country=&department="))
        rows = [
            row for row in payload.get("jobs", [])
            if str((row.get("location") or {}).get("country") or "").casefold() in {"lk", "lka"}
        ]
        self.last_inventory_count = len(rows)
        jobs: list[Job] = []
        for row in rows:
            title = str(row.get("name") or "").strip()
            if not job_title_matches_profile(profile, title):
                continue
            job_id = str(row.get("id") or "")
            detail_url = str(row.get("ref") or "")
            detail = json.loads(_get(detail_url)) if detail_url else row
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            description = " ".join(
                _clean_html(str(section.get("text") or ""))
                for section in sections.values()
                if isinstance(section, dict)
            ).strip()
            location = row.get("location") or {}
            jobs.append(Job(
                source=self.name,
                source_id=job_id,
                title=title,
                company="IFS",
                location=str(location.get("fullLocation") or "Sri Lanka"),
                country_hint="sri lanka",
                url=f"https://jobs.smartrecruiters.com/IFS1/{job_id}",
                description=description,
                published_at=str(row.get("releasedDate") or ""),
                job_type=str((row.get("typeOfEmployment") or {}).get("label") or ""),
                detail_page_verified=bool(description),
            ))
            if len(jobs) >= limit:
                break
        return jobs


class InforCareerProvider(JobProvider):
    name = "Infor Sri Lanka Careers"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        rows = json.loads(_get("https://careers.infor.com/postings.json")).get("data", [])
        sri_lanka_rows = [
            row for row in rows
            if any(term in str(row.get("location") or "").casefold() for term in ("sri lanka", "colombo"))
        ]
        self.last_inventory_count = len(sri_lanka_rows)
        jobs: list[Job] = []
        for row in sri_lanka_rows:
            title = str(row.get("title") or row.get("job") or "").strip()
            if not job_title_matches_profile(profile, title):
                continue
            description = " ".join(_clean_html(str(row.get(key) or "")) for key in (
                "description", "key_responsibilities", "skills_knowledge_expertise", "benefits"
            )).strip()
            path = str(row.get("path") or "")
            url = str(row.get("url") or urljoin("https://careers.infor.com/", path))
            jobs.append(Job(
                source=self.name,
                source_id=str(row.get("id") or path),
                title=title,
                company="Infor",
                location=str(row.get("location") or "Sri Lanka"),
                country_hint="sri lanka",
                url=url,
                description=description,
                job_type=str(row.get("employment_type_text") or row.get("employment_type") or ""),
                detail_page_verified=bool(description),
            ))
            if len(jobs) >= limit:
                break
        return jobs


class BistecZohoCareerProvider(JobProvider):
    name = "Bistec Global Careers"
    endpoint = "https://bistecglobal.zohorecruit.com/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        rows = json.loads(_get(self.endpoint)).get("data", [])
        sri_lanka_rows = [
            row for row in rows
            if not row.get("Country") or "sri lanka" in str(row.get("Country") or "").casefold()
        ]
        self.last_inventory_count = len(sri_lanka_rows)
        jobs: list[Job] = []
        for row in sri_lanka_rows:
            title = str(row.get("Posting_Title") or row.get("Job_Opening_Name") or "").strip()
            if not job_title_matches_profile(profile, title):
                continue
            job_id = str(row.get("id") or "")
            jobs.append(Job(
                source=self.name,
                source_id=job_id,
                title=title,
                company="Bistec Global",
                location=", ".join(filter(None, (str(row.get("City") or ""), str(row.get("Country") or "Sri Lanka")))),
                country_hint="sri lanka",
                url=str(row.get("$url") or "https://bistecglobal.com/careers/"),
                description=_clean_html(str(row.get("Job_Description") or "")),
                published_at=str(row.get("Date_Opened") or ""),
                job_type=str(row.get("Job_Type") or ""),
                detail_page_verified=bool(row.get("Job_Description")),
            ))
            if len(jobs) >= limit:
                break
        return jobs


class ThreeCsCareerProvider(JobProvider):
    name = "3CS Careers"

    def search(self, profile: CandidateProfile, country: str, limit: int) -> list[Job]:
        rows = json.loads(_get("https://www.3cs.lk/Careers.json"))
        self.last_inventory_count = len(rows)
        jobs: list[Job] = []
        for index, row in enumerate(rows):
            title = _clean_html(str(row.get("title") or row.get("title_blue") or ""))
            if not job_title_matches_profile(profile, title):
                continue
            description = " ".join(_clean_html(str(row.get(key) or "")) for key in (
                "description", "requirements", "additional", "tags"
            )).strip()
            jobs.append(Job(
                source=self.name,
                source_id=str(row.get("applyLink") or f"3cs-{index}"),
                title=title,
                company="3CS",
                location="Sri Lanka",
                country_hint="sri lanka",
                url=urljoin("https://www.3cs.lk/careers", str(row.get("applyLink") or "")),
                description=description,
                detail_page_verified=bool(description),
            ))
            if len(jobs) >= limit:
                break
        return jobs


def _get(url: str) -> str:
    with urlopen(Request(url, headers=_HEADERS), timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean_html(value: str) -> str:
    value = re.sub(r"<(?:br|/p|/li|/div|/h\d)\b[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(value).split())
