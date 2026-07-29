from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class CandidateProfile:
    raw_text: str
    name: str = ""
    email: str = ""
    phone: str = ""
    links: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    likely_titles: tuple[str, ...] = ()
    experience_lines: tuple[str, ...] = ()
    target_position: str = ""
    experience_years: float | None = None


@dataclass(frozen=True)
class Job:
    source: str
    source_id: str
    title: str
    company: str
    location: str
    country_hint: str
    url: str
    description: str
    published_at: str = ""
    salary: str = ""
    job_type: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail_page_verified: bool = False


@dataclass(frozen=True)
class MatchResult:
    job: Job
    score: float
    matched_skills: tuple[str, ...]
    matched_title_terms: tuple[str, ...]
    concerns: tuple[str, ...] = ()
    llm_decision: str = ""
    llm_reason: str = ""
    llm_provider: str = ""
    llm_model: str = ""


@dataclass(frozen=True)
class ContactLead:
    company: str
    contact_name: str
    title: str
    email: str
    email_type: str
    company_linkedin_search_url: str
    linkedin_search_url: str
    profile_url: str
    profile_image_url: str
    source_url: str
    search_query: str
    evidence: str
    confidence: str
