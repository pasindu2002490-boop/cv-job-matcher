from __future__ import annotations

import html
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from .models import ContactLead, MatchResult

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
HR_TERMS = (
    "recruiter",
    "recruitment",
    "talent acquisition",
    "talent partner",
    "human resources",
    "people operations",
    "people partner",
    "hr ",
    " hr",
)
GENERIC_EMAIL_PREFIXES = {
    "careers",
    "career",
    "cv",
    "hr",
    "jobs",
    "job",
    "people",
    "recruit",
    "recruiter",
    "recruiters",
    "recruiting",
    "recruitment",
    "talent",
    "work",
}


def enrich_company_contacts(
    matches: list[MatchResult],
    country: str,
    limit_companies: int,
    results_per_query: int,
    include_public_personal_emails: bool = False,
) -> tuple[list[ContactLead], list[str]]:
    if limit_companies <= 0:
        return [], ["Contact enrichment: skipped (company limit is 0)"]

    companies = _unique_companies(matches)[:limit_companies]
    if not companies:
        return [], ["Contact enrichment: skipped (no matched companies)"]

    notes: list[str] = []
    leads: list[ContactLead] = []
    google_ready = bool(
        (os.getenv("GOOGLE_CSE_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip())
        and os.getenv("GOOGLE_CSE_ID", "").strip()
    )
    if not google_ready:
        notes.append("Contact enrichment: Google CSE disabled (set GOOGLE_CSE_API_KEY/GOOGLE_API_KEY and GOOGLE_CSE_ID)")

    for company in companies:
        manual = _manual_linkedin_lead(company, country)
        leads.append(manual)
        if not google_ready:
            continue

        results, fatal_error = _search_company_contacts(company, country, results_per_query, notes)
        if fatal_error:
            google_ready = False
        for result in results:
            leads.extend(_leads_from_search_result(company, country, result, include_public_personal_emails))

    deduped = _dedupe_leads(leads)
    notes.append(f"Contact enrichment: wrote {len(deduped)} leads for {len(companies)} companies")
    if not include_public_personal_emails:
        notes.append("Contact enrichment: personal emails are excluded unless --include-public-personal-emails is used")
    return deduped, notes


def _unique_companies(matches: list[MatchResult]) -> list[str]:
    seen: set[str] = set()
    companies: list[str] = []
    for match in matches:
        company = match.job.company.strip()
        key = company.lower()
        if not company or key in {"unknown", "not supplied"} or key in seen:
            continue
        seen.add(key)
        companies.append(company)
    return companies


def _manual_linkedin_lead(company: str, country: str) -> ContactLead:
    query = _linkedin_people_query(company, country)
    return ContactLead(
        company=company,
        contact_name="",
        title="LinkedIn recruiter/HR people search",
        email="",
        email_type="",
        company_linkedin_search_url=_linkedin_company_search_url(company),
        linkedin_search_url=_linkedin_people_search_url_from_query(query),
        profile_url="",
        profile_image_url="",
        source_url="",
        search_query=query,
        evidence="Manual review link. Use LinkedIn search to verify current HR/recruiting contacts.",
        confidence="manual_review",
    )


def _linkedin_people_query(company: str, country: str) -> str:
    return f'"{company}" ("Recruiter" OR "Talent Acquisition" OR "Human Resources" OR HR) "{country}"'


def _linkedin_people_search_url(company: str, country: str) -> str:
    return _linkedin_people_search_url_from_query(_linkedin_people_query(company, country))


def _linkedin_people_search_url_from_query(query: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(query)}"


def _linkedin_company_search_url(company: str) -> str:
    query = f'"{company}"'
    return f"https://www.linkedin.com/search/results/companies/?keywords={quote_plus(query)}"


def _search_company_contacts(
    company: str,
    country: str,
    results_per_query: int,
    notes: list[str],
) -> tuple[list[dict[str, str]], bool]:
    queries = [
        f'site:linkedin.com/in "{company}" ("Recruiter" OR "Talent Acquisition" OR "Human Resources" OR HR) "{country}"',
        f'site:linkedin.com/company "{company}" "{country}"',
        f'"{company}" ("careers" OR "jobs" OR "recruitment" OR "human resources" OR HR) email "{country}"',
        f'"{company}" ("talent acquisition" OR recruiter OR recruitment) email',
    ]
    results: list[dict[str, str]] = []
    fatal_error = False
    for query in queries:
        try:
            results.extend(_google_search(query, results_per_query))
        except HTTPError as exc:
            notes.append(f"Contact enrichment: Google contact search failed for {company} ({exc})")
            fatal_error = exc.code in {400, 401, 403}
            break
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            notes.append(f"Contact enrichment: Google contact search failed for {company} ({exc})")
            break
    return results, fatal_error


def _google_search(query: str, limit: int) -> list[dict[str, str]]:
    api_key = os.getenv("GOOGLE_CSE_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    cx = os.getenv("GOOGLE_CSE_ID", "").strip()
    params = urlencode({"key": api_key, "cx": cx, "q": query, "num": max(1, min(limit, 10))})
    request = Request(
        f"https://www.googleapis.com/customsearch/v1?{params}",
        headers={"User-Agent": "cv-job-matcher/0.1"},
    )
    with urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    items = payload.get("items", [])
    return [
        {
            "title": str(item.get("title", "")),
            "snippet": str(item.get("snippet", "")),
            "link": str(item.get("link", "")),
            "thumbnail": _google_result_thumbnail(item),
            "query": query,
        }
        for item in items
        if isinstance(item, dict)
    ]


def _google_result_thumbnail(item: dict) -> str:
    pagemap = item.get("pagemap", {})
    if not isinstance(pagemap, dict):
        return ""
    thumbnails = pagemap.get("cse_thumbnail", [])
    if not isinstance(thumbnails, list) or not thumbnails:
        return ""
    first = thumbnails[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("src", ""))


def _leads_from_search_result(
    company: str,
    country: str,
    result: dict[str, str],
    include_public_personal_emails: bool,
) -> list[ContactLead]:
    title = _clean_text(result.get("title", ""))
    snippet = _clean_text(result.get("snippet", ""))
    link = result.get("link", "").strip()
    thumbnail = result.get("thumbnail", "").strip()
    search_query = result.get("query", "").strip()
    company_linkedin_search_url = _linkedin_company_search_url(company)
    linkedin_search_url = _linkedin_people_search_url(company, country)
    leads: list[ContactLead] = []

    if "linkedin.com/in" in link.lower():
        name, role = _parse_linkedin_title(title)
        if _looks_like_hr_contact(role, snippet):
            leads.append(
                ContactLead(
                    company=company,
                    contact_name=name,
                    title=role,
                    email="",
                    email_type="",
                    company_linkedin_search_url=company_linkedin_search_url,
                    linkedin_search_url=linkedin_search_url,
                    profile_url=link,
                    profile_image_url=thumbnail,
                    source_url=link,
                    search_query=search_query,
                    evidence=snippet[:300],
                    confidence="public_linkedin_result",
                )
            )

    combined_text = f"{title}\n{snippet}"
    emails = _extract_allowed_emails(combined_text, include_public_personal_emails)
    if not emails and link and "linkedin.com/" not in link.lower():
        page_text = _fetch_public_page_text(link)
        emails = _extract_allowed_emails(page_text, include_public_personal_emails)
        if page_text and _looks_like_hr_contact(title, page_text):
            combined_text = page_text[:500]

    for email, email_type in emails:
        leads.append(
            ContactLead(
                company=company,
                contact_name="",
                title="Recruiting/contact email found on public web",
                email=email,
                email_type=email_type,
                company_linkedin_search_url=company_linkedin_search_url,
                linkedin_search_url=linkedin_search_url,
                profile_url="",
                profile_image_url=thumbnail,
                source_url=link,
                search_query=search_query,
                evidence=combined_text[:300],
                confidence="public_email",
            )
        )
    return leads


def _parse_linkedin_title(title: str) -> tuple[str, str]:
    cleaned = title.replace(" | LinkedIn", "").replace(" - LinkedIn", "")
    parts = [part.strip() for part in re.split(r"\s[-|]\s", cleaned, maxsplit=1) if part.strip()]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _looks_like_hr_contact(title: str, text: str) -> bool:
    haystack = f" {title} {text} ".lower()
    return any(term in haystack for term in HR_TERMS)


def _extract_allowed_emails(text: str, include_public_personal_emails: bool) -> list[tuple[str, str]]:
    seen: set[str] = set()
    emails: list[tuple[str, str]] = []
    for email in EMAIL_RE.findall(text or ""):
        normalized = email.strip(".,;:()[]{}<>").lower()
        if normalized in seen or _is_placeholder_email(normalized):
            continue
        seen.add(normalized)
        email_type = _email_type(normalized)
        if email_type == "public_personal" and not include_public_personal_emails:
            continue
        emails.append((normalized, email_type))
    return emails


def _email_type(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    local_root = re.split(r"[._+-]", local, maxsplit=1)[0]
    if local in GENERIC_EMAIL_PREFIXES or local_root in GENERIC_EMAIL_PREFIXES:
        return "generic_recruiting"
    return "public_personal"


def _is_placeholder_email(email: str) -> bool:
    return any(token in email for token in ("example.com", "domain.com", "email.com", "yourname", "name@"))


def _fetch_public_page_text(url: str) -> str:
    try:
        request = Request(url, headers={"User-Agent": "cv-job-matcher/0.1"})
        with urlopen(request, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return ""
            raw = response.read(250_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, ValueError):
        return ""
    without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    return _clean_text(text)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _dedupe_leads(leads: list[ContactLead]) -> list[ContactLead]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[ContactLead] = []
    for lead in leads:
        key = (
            lead.company.lower().strip(),
            lead.contact_name.lower().strip(),
            lead.email.lower().strip(),
            lead.profile_url.lower().strip() or lead.linkedin_search_url.lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(lead)
    return deduped
