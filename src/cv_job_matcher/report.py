from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .models import CandidateProfile, ContactLead, Job, MatchResult

SRI_LANKA_SOURCE_AUDIT = [
    ("topjobs.lk", "integrated", "Public vacancy HTML parser."),
    ("XpressJobs", "integrated", "Public JSON endpoint discovered from app bundle."),
    ("ikmanJOBS", "pending", "Public category pages checked; needs custom parser/API discovery."),
    ("Jobber.lk", "pending", "Needs custom parser/API discovery."),
    ("JobFactory.lk", "pending", "Search URL checked; returned 404 for simple query shape."),
    ("DreamJobs.lk", "pending", "Public HTML checked; needs custom search/result parser."),
    ("JobEka.lk", "pending", "Needs custom parser/API discovery."),
    ("FindMyJob.lk", "pending", "Needs custom parser/API discovery."),
    ("LinkedIn Jobs Sri Lanka", "integrated/limited", "Public guest pages parsed; may rate-limit."),
    ("Career141", "pending", "Public page checked; Next.js data/parser work needed."),
    ("CareerFirst.lk", "pending", "Needs custom parser/API discovery."),
    ("ObserverJobs.lk", "pending", "Needs custom parser/API discovery."),
    ("TimesJobs.lk", "pending", "Needs custom parser/API discovery."),
    ("GovernmentJobs.lk", "pending", "Government/public-sector jobs; role-specific parser needed."),
    ("GovernmentVacancies.lk", "pending", "Government/public-sector jobs; role-specific parser needed."),
    ("Gazette.lk", "pending", "Gazette/public-sector notices; not a normal job API."),
    ("job.govdoc.lk", "pending", "Government documents; role extraction parser needed."),
    ("SLBFE Job Bank", "pending", "Foreign employment bank; role extraction parser needed."),
    ("LankaQualityJobs.com", "pending", "Needs custom parser/API discovery."),
    ("Recruitme.lk", "pending", "Needs custom parser/API discovery."),
    ("Jobpal.lk", "pending", "Needs custom parser/API discovery."),
    ("Jobup.lk", "pending", "Needs custom parser/API discovery."),
    ("MYJOBS.LK", "pending", "Needs custom parser/API discovery."),
    ("ITPro.lk", "integrated", "AI/Data RSS feed."),
    ("RemoteRocketship", "integrated/remote opt-in", "Sri Lanka remote role pages via structured data; included with --include-remote-global."),
    ("DuckDuckGo", "integrated", "Search-discovery mode via --web-discovery."),
    ("Google Custom Search", "optional", "Set GOOGLE_CSE_API_KEY/GOOGLE_API_KEY and GOOGLE_CSE_ID."),
    ("SerpAPI Google", "optional", "Set SERPAPI_API_KEY."),
    ("Crawl4AI", "optional hook", "Set CRAWL4AI_ENABLED=1 and CRAWL4AI_SEED_URLS after installing crawl4ai."),
    ("Company contact enrichment", "optional", "Run with --find-contacts. Uses public evidence, Google CSE when configured, and LinkedIn people-search links for manual verification."),
]


def write_outputs(
    out_dir: Path,
    profile: CandidateProfile,
    country: str,
    jobs: list[Job],
    matches: list[MatchResult],
    provider_notes: list[str],
    tailored_cv: str,
    contact_leads: list[ContactLead] | None = None,
) -> None:
    contact_leads = contact_leads or []
    out_dir.mkdir(parents=True, exist_ok=True)
    # These legacy CSVs are no longer part of the public report set. Remove
    # stale copies when a caller reuses an existing output directory.
    for legacy_name in ("companies_hiring.csv", "company_contacts.csv"):
        (out_dir / legacy_name).unlink(missing_ok=True)
    (out_dir / "tailored_cv.md").write_text(tailored_cv, encoding="utf-8")
    _write_all_jobs_csv(out_dir / "all_discovered_jobs.csv", jobs)
    contact_by_company = _best_contact_by_company(contact_leads)
    _write_csv(out_dir / "job_matches.csv", matches, contact_by_company)
    (out_dir / "job_matches.md").write_text(
        _build_markdown_report(profile, country, matches, provider_notes),
        encoding="utf-8",
    )
    (out_dir / "companies_hiring.md").write_text(
        _build_companies_report(country, matches),
        encoding="utf-8",
    )
    (out_dir / "company_contacts.md").write_text(
        _build_contacts_report(country, contact_leads),
        encoding="utf-8",
    )
    (out_dir / "source_audit.md").write_text(_build_source_audit(), encoding="utf-8")


def _write_all_jobs_csv(path: Path, jobs: list[Job]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "title",
                "company",
                "location",
                "country_hint",
                "source",
                "published_at",
                "fetched_at_utc",
                "job_type",
                "salary",
                "apply_url",
            ]
        )
        for job in jobs:
            writer.writerow(
                [
                    job.title,
                    job.company,
                    job.location,
                    job.country_hint,
                    job.source,
                    job.published_at,
                    job.fetched_at.isoformat(),
                    job.job_type,
                    job.salary,
                    job.url,
                ]
            )


def _write_csv(path: Path, matches: list[MatchResult], contact_by_company: dict[str, ContactLead]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "match_score",
                "title",
                "company",
                "location",
                "source",
                "published_at",
                "fetched_at_utc",
                "matched_skills",
                "concerns",
                "llm_decision",
                "llm_reason",
                "hr_contact_name",
                "hr_title",
                "hr_email",
                "hr_email_type",
                "hr_contact_confidence",
                "company_linkedin_search_url",
                "hr_linkedin_search_url",
                "hr_profile_url",
                "hr_profile_image_url",
                "hr_source_url",
                "hr_search_query",
                "apply_url",
            ]
        )
        for match in matches:
            job = match.job
            contact = contact_by_company.get(_company_key(job.company))
            writer.writerow(
                [
                    match.score,
                    job.title,
                    job.company,
                    job.location,
                    job.source,
                    job.published_at,
                    job.fetched_at.isoformat(),
                    ", ".join(match.matched_skills),
                    " ".join(match.concerns),
                    match.llm_decision,
                    match.llm_reason,
                    contact.contact_name if contact else "",
                    contact.title if contact else "",
                    contact.email if contact else "",
                    contact.email_type if contact else "",
                    contact.confidence if contact else "",
                    contact.company_linkedin_search_url if contact else "",
                    contact.linkedin_search_url if contact else "",
                    contact.profile_url if contact else "",
                    contact.profile_image_url if contact else "",
                    contact.source_url if contact else "",
                    contact.search_query if contact else "",
                    job.url,
                ]
            )


def _write_companies_csv(path: Path, matches: list[MatchResult], contact_by_company: dict[str, ContactLead]) -> None:
    seen = set()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "company",
                "title",
                "score",
                "location",
                "source",
                "published_at",
                "hr_contact_name",
                "hr_title",
                "hr_email",
                "hr_email_type",
                "hr_contact_confidence",
                "company_linkedin_search_url",
                "hr_linkedin_search_url",
                "hr_profile_url",
                "hr_profile_image_url",
                "hr_source_url",
                "hr_search_query",
                "apply_url",
            ]
        )
        for match in matches:
            job = match.job
            key = (job.company.lower().strip(), job.title.lower().strip(), job.url.lower().strip())
            if key in seen:
                continue
            seen.add(key)
            contact = contact_by_company.get(_company_key(job.company))
            writer.writerow(
                [
                    job.company,
                    job.title,
                    match.score,
                    job.location,
                    job.source,
                    job.published_at,
                    contact.contact_name if contact else "",
                    contact.title if contact else "",
                    contact.email if contact else "",
                    contact.email_type if contact else "",
                    contact.confidence if contact else "",
                    contact.company_linkedin_search_url if contact else "",
                    contact.linkedin_search_url if contact else "",
                    contact.profile_url if contact else "",
                    contact.profile_image_url if contact else "",
                    contact.source_url if contact else "",
                    contact.search_query if contact else "",
                    job.url,
                ]
            )


def _write_contacts_csv(path: Path, leads: list[ContactLead]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "company",
                "contact_name",
                "title",
                "email",
                "email_type",
                "confidence",
                "company_linkedin_search_url",
                "linkedin_search_url",
                "profile_url",
                "profile_image_url",
                "source_url",
                "search_query",
                "evidence",
            ]
        )
        for lead in leads:
            writer.writerow(
                [
                    lead.company,
                    lead.contact_name,
                    lead.title,
                    lead.email,
                    lead.email_type,
                    lead.confidence,
                    lead.company_linkedin_search_url,
                    lead.linkedin_search_url,
                    lead.profile_url,
                    lead.profile_image_url,
                    lead.source_url,
                    lead.search_query,
                    lead.evidence,
                ]
            )


def _best_contact_by_company(leads: list[ContactLead]) -> dict[str, ContactLead]:
    priority = {
        "public_email": 0,
        "public_linkedin_result": 1,
        "manual_review": 2,
    }
    best: dict[str, ContactLead] = {}
    for lead in leads:
        key = _company_key(lead.company)
        current = best.get(key)
        if current is None or priority.get(lead.confidence, 99) < priority.get(current.confidence, 99):
            best[key] = lead
    return best


def _company_key(company: str) -> str:
    return " ".join(company.lower().strip().split())


def _build_markdown_report(
    profile: CandidateProfile,
    country: str,
    matches: list[MatchResult],
    provider_notes: list[str],
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Job Match Report",
        "",
        f"- Country: {country}",
        f"- Generated at UTC: {now}",
        f"- Candidate: {profile.name or 'Unknown'}",
        f"- Target position: {profile.target_position or 'Inferred from CV'}",
        f"- Experience years: {profile.experience_years if profile.experience_years is not None else 'Not supplied'}",
        f"- Extracted skills: {', '.join(profile.skills) if profile.skills else 'None detected'}",
        "",
        "## Source Coverage",
    ]
    for note in provider_notes:
        lines.append(f"- {note}")
    lines.extend(
        [
            "",
            "## Accuracy Boundary",
            "This report only contains jobs returned by the configured live sources during this run. It is not a complete inventory of every employer or job board in the country.",
            "",
            "## Ranked Matches",
        ]
    )
    if not matches:
        lines.append("No matches passed the minimum score. Try adding more skills to the CV or enabling Adzuna credentials.")
    for index, match in enumerate(matches, start=1):
        job = match.job
        lines.extend(
            [
                "",
                f"### {index}. {job.title} - {job.company}",
                f"- Score: {match.score}",
                f"- Location: {job.location}",
                f"- Source: {job.source}",
                f"- Published: {job.published_at or 'Not supplied'}",
                f"- Fetched UTC: {job.fetched_at.isoformat()}",
                f"- Matched skills: {', '.join(match.matched_skills) or 'None'}",
                f"- Apply: {job.url}",
            ]
        )
        if match.llm_decision or match.llm_reason:
            lines.append(f"- LLM: {match.llm_decision or 'review'} - {match.llm_reason}")
        if match.concerns:
            lines.append(f"- Manual checks: {' '.join(match.concerns)}")
    return "\n".join(lines).strip() + "\n"


def _build_companies_report(country: str, matches: list[MatchResult]) -> str:
    lines = [
        "# Companies Hiring",
        "",
        f"- Country filter: {country}",
        "- Scope: companies with currently open matching roles returned by configured live sources",
        "",
    ]
    seen = set()
    for index, match in enumerate(matches, start=1):
        job = match.job
        key = (job.company.lower().strip(), job.title.lower().strip(), job.url.lower().strip())
        if key in seen:
            continue
        seen.add(key)
        lines.extend(
            [
                f"## {index}. {job.company or 'Unknown company'}",
                f"- Role: {job.title}",
                f"- Score: {match.score}",
                f"- Location: {job.location}",
                f"- Source: {job.source}",
                f"- Apply: {job.url}",
                "",
            ]
        )
    if not seen:
        lines.append("No matching companies found.")
    return "\n".join(lines).strip() + "\n"


def _build_contacts_report(country: str, leads: list[ContactLead]) -> str:
    lines = [
        "# Company Contacts",
        "",
        f"- Country filter: {country}",
        "- Scope: public recruiting/contact evidence plus LinkedIn people-search links for manual verification",
        "- Privacy boundary: no private LinkedIn scraping and no guessed email patterns",
        "",
    ]
    if not leads:
        lines.append("No contact leads were generated. Run with --find-contacts to enable enrichment.")
        return "\n".join(lines).strip() + "\n"

    for lead in leads:
        lines.extend(
            [
                f"## {lead.company}",
                f"- Contact: {lead.contact_name or 'Manual review'}",
                f"- Title: {lead.title or 'Not supplied'}",
                f"- Email: {lead.email or 'Not found'}",
                f"- Email type: {lead.email_type or 'N/A'}",
                f"- Confidence: {lead.confidence}",
                f"- LinkedIn company search: {lead.company_linkedin_search_url}",
                f"- LinkedIn search: {lead.linkedin_search_url}",
            ]
        )
        if lead.profile_url:
            lines.append(f"- Public profile result: {lead.profile_url}")
        if lead.profile_image_url:
            lines.append(f"- Public result image: {lead.profile_image_url}")
        if lead.source_url:
            lines.append(f"- Source: {lead.source_url}")
        if lead.search_query:
            lines.append(f"- Search query: {lead.search_query}")
        if lead.evidence:
            lines.append(f"- Evidence: {lead.evidence}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _build_source_audit() -> str:
    lines = [
        "# Source Audit",
        "",
        "Google-style result counts may include duplicates, cached pages, expired roles, global remote roles, and pages that require login or JavaScript. This tool records only retrievable openings with URLs from configured sources.",
        "",
    ]
    for site, status, notes in SRI_LANKA_SOURCE_AUDIT:
        lines.append(f"- {site}: {status}. {notes}")
    return "\n".join(lines).strip() + "\n"
