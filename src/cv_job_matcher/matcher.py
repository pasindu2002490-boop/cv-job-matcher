from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .country import normalize_country
from .models import CandidateProfile, Job, MatchResult

HIGH_VALUE_SKILLS = {
    "aws",
    "azure",
    "c#",
    "ci/cd",
    "cloud",
    "data analysis",
    "data engineering",
    "devops",
    "django",
    "docker",
    "fastapi",
    "java",
    "javascript",
    "kubernetes",
    "langchain",
    "linux",
    "llm",
    "machine learning",
    "mlops",
    "nlp",
    "node",
    "postgresql",
    "python",
    "rag",
    "react",
    "sql",
    "typescript",
}

LOW_SIGNAL_SKILLS = {
    "agile",
    "communication",
    "excel",
    "finance",
    "leadership",
    "sales",
    "scrum",
}

COUNTRY_ALIASES = {
    "sri lanka": ("sri lanka", "colombo", "kandy", "galle", "jaffna", "rajagiriya"),
    "united kingdom": ("united kingdom", "uk", "england", "scotland", "wales", "northern ireland"),
    "united states": ("united states", "usa", "u.s.", "us"),
}

GLOBAL_LOCATIONS = ("worldwide", "anywhere", "global", "all regions", "apac", "asia")

# These connectors enumerate the source's current/open inventory on every cache
# refresh. A posting can remain open longer than the generic age window, so its
# continued presence is stronger evidence than its original publication date.
CURRENT_OPEN_INVENTORY_SOURCES = {
    "Adzuna",
    "Arbeitnow",
    "CareerFirst",
    "Career141",
    "CareerLK",
    "Crawl4AI Seeds",
    "CSE Careers",
    "DreamJobs.lk",
    "FindMyJob.lk",
    "Gazette.lk",
    "Government Jobs",
    "GovernmentJobs.lk",
    "GovernmentVacancies.lk",
    "Hire.lk",
    "Himalayas",
    "Ikman Jobs",
    "Inseeks",
    "ITPro.lk",
    "JobEka.lk",
    "JobFactory.lk",
    "Jobber.lk",
    "JobPal",
    "Jobup.lk",
    "job.govdoc.lk",
    "LankaJob.lk",
    "LankaQualityJobs.com",
    "LinkedIn Public",
    "MYJOBS.LK",
    "Observer Jobs",
    "Recruiter.lk",
    "Recruitme.lk",
    "Remote OK",
    "Remotive",
    "RemoteRocketship",
    "SLBFE Job Bank",
    "TimesJobs.lk",
    "topjobs.lk",
    "We Work Remotely",
    "XpressJobs",
}


def filter_country_compatible(
    jobs: list[Job], country: str, allow_global_remote: bool = False
) -> tuple[list[Job], int]:
    """Fail closed when a listing explicitly points outside the selected country."""
    target = normalize_country(country)
    aliases = COUNTRY_ALIASES.get(target, (target,))
    kept = []
    for job in jobs:
        location = job.location.strip().lower()
        hint = normalize_country(job.country_hint)
        target_in_location = bool(
            location and any(_contains_location_term(location, alias) for alias in aliases)
        )
        other_country = _mentions_other_country(location, target)
        global_location = any(
            _contains_location_term(location, term) for term in GLOBAL_LOCATIONS
        )
        remote_location = bool(re.search(r"\bremote\b", location))

        # Worldwide-remote opt-in is not permission for foreign on-site or
        # country-restricted remote vacancies.
        if allow_global_remote and global_location and not other_country:
            kept.append(job)
            continue
        if (
            allow_global_remote
            and remote_location
            and not other_country
            and (not hint or hint == target or target_in_location)
        ):
            kept.append(job)
            continue
        if global_location:
            continue
        if location in {"remote", "remote only"}:
            if hint == target:
                kept.append(job)
            continue
        if target_in_location:
            kept.append(job)
            continue
        if not location and hint == target:
            kept.append(job)
            continue
        # Country-specific providers commonly return a city without a country.
        if hint == target and not _mentions_other_country(location, target):
            kept.append(job)
    return kept, len(jobs) - len(kept)


def filter_fresh_jobs(
    jobs: list[Job], max_age_days: int = 7, now: datetime | None = None
) -> tuple[list[Job], int]:
    """Keep live listings and reject stale/unverifiable discovery-only records."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max_age_days)
    kept = []
    for job in jobs:
        text = f"{job.title} {job.description}".lower()
        if any(
            term in text
            for term in (
                "applications are closed",
                "application deadline has passed",
                "job expired",
                "job has expired",
                "job is no longer available",
                "no longer accepting applications",
                "position filled",
                "position has been filled",
                "vacancy has expired",
            )
        ):
            continue
        published = _parse_job_date(job.published_at)
        if published and published > current + timedelta(days=1):
            continue
        if (
            published
            and published < cutoff
            and job.source not in CURRENT_OPEN_INVENTORY_SOURCES
        ):
            continue
        if (
            not published
            and job.source
            in {
                "DuckDuckGo Discovery",
                "Google Custom Search",
                "SerpAPI Google",
                "Crawl4AI",
                "Crawl4AI Seeds",
            }
            and not job.detail_page_verified
        ):
            continue
        kept.append(job)
    return kept, len(jobs) - len(kept)


def _parse_job_date(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mentions_other_country(location: str, target: str) -> bool:
    known = {
        "australia", "bahrain", "bangladesh", "canada", "china", "france",
        "germany", "hong kong", "india", "indonesia", "ireland", "italy",
        "japan", "kuwait", "malaysia", "maldives", "nepal", "netherlands",
        "new zealand", "oman", "pakistan", "philippines", "poland", "qatar",
        "saudi arabia", "singapore", "spain", "thailand", "united arab emirates",
        "united kingdom", "united states", "usa", "uk", "vietnam",
    }
    target_aliases = set(COUNTRY_ALIASES.get(target, (target,)))
    return any(
        country not in target_aliases
        and _contains_location_term(location, country)
        for country in known
    )


def _contains_location_term(location: str, term: str) -> bool:
    escaped = re.escape(term.casefold()).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z]){escaped}(?![a-z])", location.casefold()))


def rank_jobs(profile: CandidateProfile, jobs: list[Job], minimum_score: float = 20.0) -> list[MatchResult]:
    results = [_score_job(profile, job) for job in jobs]
    results = [result for result in results if result.score >= minimum_score]
    return sorted(results, key=lambda item: item.score, reverse=True)


def filter_experience_compatible(
    profile: CandidateProfile, matches: list[MatchResult]
) -> tuple[list[MatchResult], int]:
    """Remove jobs whose title or stated requirement exceeds supplied experience."""
    if profile.experience_years is None:
        return matches, 0
    kept = []
    for match in matches:
        required = required_experience_years(match.job)
        if required is None or required <= profile.experience_years:
            kept.append(match)
    return kept, len(matches) - len(kept)


def required_experience_years(job: Job) -> float | None:
    """Return a conservative minimum based on title seniority and explicit text."""
    title = job.title.lower()
    entry_terms = ("intern", "trainee", "graduate", "junior", "entry level", "entry-level")
    title_minimum = 0.0 if any(term in title for term in entry_terms) else None
    seniority_rules = (
        (r"\b(?:vice president|vp|director|head of|chief)\b", 7.0),
        (r"\b(?:principal|staff|architect|manager)\b", 5.0),
        (r"\b(?:lead|senior|sr\.?)(?:\s|$)", 3.0),
    )
    if title_minimum is None:
        for pattern, years in seniority_rules:
            if re.search(pattern, title):
                title_minimum = years
                break

    text = f"{job.title}. {job.description}".lower()
    explicit = []
    patterns = (
        r"(?:minimum|min\.?|at least|requires?|required|with)\s+(\d+(?:\.\d+)?)\+?\s*(?:-|to)?\s*\d*\s*years?",
        r"(\d+(?:\.\d+)?)\+?\s*(?:-|to)?\s*\d*\s*years?\s+(?:of\s+)?(?:relevant\s+|professional\s+)?experience",
    )
    for pattern in patterns:
        explicit.extend(float(value) for value in re.findall(pattern, text) if float(value) <= 60)
    requirements = explicit + ([title_minimum] if title_minimum is not None else [])
    return max(requirements) if requirements else None


def _score_job(profile: CandidateProfile, job: Job) -> MatchResult:
    haystack = f"{job.title} {job.description}".lower()
    title = job.title.lower()
    matched_skills = tuple(skill for skill in profile.skills if _contains_term(haystack, skill))
    matched_titles = tuple(term for term in profile.likely_titles if _contains_term(job.title.lower(), term))
    strong_skills = tuple(skill for skill in matched_skills if skill in HIGH_VALUE_SKILLS)
    low_signal_skills = tuple(skill for skill in matched_skills if skill in LOW_SIGNAL_SKILLS)
    score = 0.0
    score += min(len(strong_skills) * 12.0, 72.0)
    score += min(len(low_signal_skills) * 2.0, 8.0)
    score += min(len(matched_titles) * 16.0, 32.0)
    score += _target_position_alignment(profile.target_position, title)
    if _has_ai_profile(profile):
        alignment = _ai_role_alignment(title)
        score += alignment
        if alignment < 0:
            score -= 20.0
    if job.published_at:
        score += 5.0
    if job.url:
        score += 5.0
    concerns = []
    if any(term in haystack for term in ("visa", "work authorization", "security clearance", "native")):
        concerns.append("Check eligibility requirements in the job description.")
    senior_role = any(term in title for term in ("senior", "lead", "principal", "staff", "manager", "director"))
    if senior_role and profile.experience_years is not None and profile.experience_years < 3:
        score -= 20.0
        concerns.append("Seniority may exceed the supplied experience years.")
    elif senior_role and not any(
        term in profile.raw_text.lower() for term in ("senior", "lead", "manager", "principal", "5 years", "6 years", "7 years")
    ):
        concerns.append("Seniority may need manual review.")
    return MatchResult(
        job=job,
        score=round(min(score, 100.0), 1),
        matched_skills=matched_skills,
        matched_title_terms=matched_titles,
        concerns=tuple(concerns),
    )


def _contains_term(text: str, term: str) -> bool:
    escaped = re.escape(term.lower()).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9+#]){escaped}(?![a-z0-9+#])", text))


def _has_ai_profile(profile: CandidateProfile) -> bool:
    ai_terms = {"ai/ml engineer", "ai engineer", "machine learning engineer"}
    ai_skills = {"machine learning", "llm", "nlp", "rag", "mlops", "langchain"}
    return bool(
        ai_terms.intersection(profile.likely_titles)
        or ai_skills.intersection(profile.skills)
        or any(term in profile.target_position.lower() for term in ("ai", "machine learning", "ml", "llm"))
    )


def _target_position_alignment(position: str, title: str) -> float:
    if not position:
        return 0.0
    target_tokens = [token for token in re.findall(r"[a-z0-9+#]+", position.lower()) if len(token) > 1]
    if not target_tokens:
        return 0.0
    if position.lower().strip() in title:
        return 30.0
    overlap = sum(1 for token in target_tokens if re.search(rf"\b{re.escape(token)}\b", title))
    if overlap == len(target_tokens):
        return 24.0
    if overlap:
        return min(12.0, overlap * 6.0)
    return -30.0


def _ai_role_alignment(title: str) -> float:
    title_text = f" {title} "
    excluded = ("sales", "copywriter", "writer", "customer", "office assistant", "designer", "video editor")
    if any(term in title_text for term in excluded):
        return -65.0
    exact_ai_engineer = (
        "ai engineer",
        "ai/ml engineer",
        "ml engineer",
        "machine learning engineer",
        "llm engineer",
        "rag engineer",
        "nlp engineer",
        "computer vision engineer",
        "ai architect",
    )
    if any(term in title_text for term in exact_ai_engineer):
        return 35.0
    if "data scientist" in title_text or "applied scientist" in title_text:
        return 25.0
    if (
        re.search(r"\bai\b", title_text)
        or "machine learning" in title_text
        or re.search(r"\bllm\b", title_text)
        or re.search(r"\bml\b", title_text)
    ):
        return 15.0
    return -45.0
