from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence, TextIO

from .models import CandidateProfile, ContactLead, Job, MatchResult

if TYPE_CHECKING:
    from .agent_graph import SourceAgentTrace


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe_cell(value: object) -> object:
    """Keep untrusted text from being evaluated as a spreadsheet formula."""
    if isinstance(value, str) and value.lstrip().startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


class _FormulaSafeCsvWriter:
    """Apply formula-injection protection at the final CSV serialization boundary."""

    def __init__(self, handle: TextIO) -> None:
        self._writer: Any = csv.writer(handle)

    def writerow(self, row: Sequence[object]) -> object:
        return self._writer.writerow([_csv_safe_cell(cell) for cell in row])


SRI_LANKA_SOURCE_AUDIT = [
    ("topjobs.lk", "integrated", "Complete current open-vacancy inventory with pagination."),
    ("XpressJobs", "integrated", "Complete active JSON inventory with record-count pagination."),
    ("Jobber.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("JobFactory.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("DreamJobs.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("JobEka.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("FindMyJob.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("LinkedIn Jobs Sri Lanka", "integrated/limited", "Public guest pages parsed; may rate-limit."),
    ("Career141", "integrated/bounded", "Public same-site inventory crawler."),
    ("CareerFirst.lk", "integrated", "Public same-site listing crawler."),
    ("ObserverJobs.lk", "integrated", "Public same-site listing crawler."),
    ("TimesJobs.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("GovernmentJobs.lk", "integrated/bounded", "Public same-site public-sector crawler."),
    ("GovernmentVacancies.lk", "integrated/bounded", "Public same-site public-sector crawler."),
    ("Gazette.lk", "integrated/bounded", "Public same-site notices crawler."),
    ("job.govdoc.lk", "integrated/bounded", "Public same-site public-sector crawler."),
    ("SLBFE Job Bank", "integrated/bounded", "Public foreign-employment inventory crawler."),
    ("LankaQualityJobs.com", "integrated/bounded", "Public HTTP same-site inventory crawler."),
    ("Recruitme.lk", "integrated/bounded", "Public same-site inventory crawler."),
    ("Jobpal.lk", "integrated", "Public same-site listing crawler."),
    ("Jobup.lk", "registered/limited", "Connector is registered; current HTTPS certificate failure is reported per run."),
    ("MYJOBS.LK", "integrated/bounded", "Public same-site inventory crawler."),
    ("ITPro.lk", "integrated", "Dynamic category inventory with pagination and local role filtering."),
    ("CareerLK", "integrated", "Public same-site listing crawler."),
    ("Hire.lk", "integrated", "Public position search and listing crawler."),
    ("Recruiter.lk", "integrated", "Public same-site listing crawler."),
    ("LankaJob.lk", "integrated", "Public same-site listing crawler."),
    ("Inseeks", "integrated", "Public same-site listing crawler."),
    ("Ikman Jobs", "integrated", "Public category/search listing crawler."),
    ("CSE Careers", "integrated", "Public careers-page crawler."),
    ("Government Jobs", "integrated", "Public vacancies-page crawler."),
    ("RemoteRocketship", "integrated/remote opt-in", "Sri Lanka remote role pages via structured data; included with --include-remote-global."),
    ("DuckDuckGo", "integrated", "Search-discovery mode via --web-discovery."),
    ("Google Custom Search", "optional", "Set GOOGLE_CSE_API_KEY/GOOGLE_API_KEY and GOOGLE_CSE_ID."),
    ("SerpAPI Google", "optional", "Set SERPAPI_API_KEY."),
    ("Crawl4AI", "optional", "Rendered bounded discovery for configured CRAWL4AI_SEED_URLS."),
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
    related_matches: list[MatchResult] | None = None,
    rejected_matches: list[MatchResult] | None = None,
    manual_review_matches: list[MatchResult] | None = None,
    source_traces: Sequence[SourceAgentTrace] | None = None,
) -> None:
    contact_leads = contact_leads or []
    related_matches = related_matches if related_matches is not None else matches
    rejected_matches = rejected_matches or []
    manual_review_matches = manual_review_matches or []
    source_traces = list(source_traces or [])
    out_dir.mkdir(parents=True, exist_ok=True)
    # These legacy CSVs are no longer part of the public report set. Remove
    # stale copies when a caller reuses an existing output directory.
    for legacy_name in ("companies_hiring.csv", "company_contacts.csv"):
        (out_dir / legacy_name).unlink(missing_ok=True)
    (out_dir / "tailored_cv.md").write_text(tailored_cv, encoding="utf-8")
    _write_all_jobs_csv(out_dir / "all_discovered_jobs.csv", jobs)
    contact_by_company = _best_contact_by_company(contact_leads)
    _write_csv(
        out_dir / "related_vacancies.csv",
        related_matches,
        {},
    )
    _write_csv(out_dir / "job_matches.csv", matches, contact_by_company)
    _write_csv(out_dir / "rejected_vacancies.csv", rejected_matches, {})
    _write_csv(
        out_dir / "manual_review_vacancies.csv",
        manual_review_matches,
        {},
    )
    _write_source_coverage_csv(
        out_dir / "source_coverage.csv",
        source_traces,
        jobs,
        related_matches,
        matches,
        rejected_matches,
        manual_review_matches,
    )
    (out_dir / "job_matches.md").write_text(
        _build_markdown_report(
            profile,
            country,
            matches,
            provider_notes,
            discovered_count=len(jobs),
            related_count=len(related_matches),
            rejected_count=len(rejected_matches),
            manual_review_count=len(manual_review_matches),
        ),
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
    (out_dir / "source_audit.md").write_text(
        _build_source_audit(
            source_traces=source_traces,
            jobs=jobs,
            related_matches=related_matches,
            matches=matches,
            rejected_matches=rejected_matches,
            manual_review_matches=manual_review_matches,
            provider_notes=provider_notes,
        ),
        encoding="utf-8",
    )


def _write_all_jobs_csv(path: Path, jobs: list[Job]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = _FormulaSafeCsvWriter(handle)
        writer.writerow(
            [
                "title",
                "company",
                "location",
                "country_hint",
                "source",
                "published_at",
                "fetched_at_utc",
                "detail_page_verified",
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
                    "yes" if job.detail_page_verified else "no",
                    job.job_type,
                    job.salary,
                    job.url,
                ]
            )


def _write_source_coverage_csv(
    path: Path,
    source_traces: Sequence[SourceAgentTrace],
    jobs: list[Job],
    related_matches: list[MatchResult],
    matches: list[MatchResult],
    rejected_matches: list[MatchResult],
    manual_review_matches: list[MatchResult],
) -> None:
    """Write machine-readable, run-specific connector and pipeline counts."""
    discovered_by_source = Counter(job.source for job in jobs)
    related_by_source = Counter(match.job.source for match in related_matches)
    final_by_source = Counter(match.job.source for match in matches)
    rejected_by_source = Counter(match.job.source for match in rejected_matches)
    manual_by_source = Counter(
        match.job.source for match in manual_review_matches
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = _FormulaSafeCsvWriter(handle)
        writer.writerow(
            [
                "source",
                "connector",
                "status",
                "full_inventory_rows",
                "role_candidate_rows_returned",
                "unique_discovered",
                "related_before_llm",
                "final_vacancies",
                "final_rejected",
                "final_manual_review",
                "note",
            ]
        )
        for trace in source_traces:
            writer.writerow(
                [
                    trace.source,
                    trace.connector,
                    trace.status,
                    "" if trace.inventory_total is None else trace.inventory_total,
                    trace.discovered,
                    discovered_by_source[trace.source],
                    related_by_source[trace.source],
                    final_by_source[trace.source],
                    rejected_by_source[trace.source],
                    manual_by_source[trace.source],
                    trace.note,
                ]
            )


def _write_csv(path: Path, matches: list[MatchResult], contact_by_company: dict[str, ContactLead]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = _FormulaSafeCsvWriter(handle)
        writer.writerow(
            [
                "match_score",
                "title",
                "company",
                "location",
                "source",
                "published_at",
                "fetched_at_utc",
                "detail_page_verified",
                "matched_skills",
                "concerns",
                "llm_decision",
                "llm_reason",
                "llm_provider",
                "llm_model",
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
                    "yes" if job.detail_page_verified else "no",
                    ", ".join(match.matched_skills),
                    " ".join(match.concerns),
                    match.llm_decision,
                    match.llm_reason,
                    match.llm_provider,
                    match.llm_model,
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
        writer = _FormulaSafeCsvWriter(handle)
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
        writer = _FormulaSafeCsvWriter(handle)
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
    discovered_count: int | None = None,
    related_count: int | None = None,
    rejected_count: int = 0,
    manual_review_count: int = 0,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    discovered_count = len(matches) if discovered_count is None else discovered_count
    related_count = len(matches) if related_count is None else related_count
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
        "## Pipeline Counts",
        f"- All discovered vacancies: {discovered_count}",
        f"- Related vacancies before final LLM review: {related_count}",
        f"- Final vacancies: {len(matches)}",
        f"- Rejected during final eligibility review: {rejected_count}",
        f"- Vacancies requiring manual review: {manual_review_count}",
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
            provenance = " / ".join(
                value for value in (match.llm_provider, match.llm_model) if value
            )
            lines.append(
                f"- LLM{f' ({provenance})' if provenance else ''}: "
                f"{match.llm_decision or 'review'} - {match.llm_reason}"
            )
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


def _build_source_audit(
    source_traces: Sequence[SourceAgentTrace] = (),
    jobs: list[Job] | None = None,
    related_matches: list[MatchResult] | None = None,
    matches: list[MatchResult] | None = None,
    rejected_matches: list[MatchResult] | None = None,
    manual_review_matches: list[MatchResult] | None = None,
    provider_notes: Sequence[str] = (),
) -> str:
    jobs = jobs or []
    related_matches = related_matches or []
    matches = matches or []
    rejected_matches = rejected_matches or []
    manual_review_matches = manual_review_matches or []
    discovered_by_source = Counter(job.source for job in jobs)
    related_by_source = Counter(match.job.source for match in related_matches)
    final_by_source = Counter(match.job.source for match in matches)
    rejected_by_source = Counter(match.job.source for match in rejected_matches)
    manual_by_source = Counter(
        match.job.source for match in manual_review_matches
    )
    status_counts = Counter(trace.status for trace in source_traces)
    rows_before_dedupe = (
        sum(trace.discovered for trace in source_traces)
        if source_traces
        else len(jobs)
    )
    consolidated_rows = max(0, rows_before_dedupe - len(jobs))
    decisioned_rows = sum(
        bool(match.llm_decision)
        for match in [
            *matches,
            *rejected_matches,
            *manual_review_matches,
        ]
    )
    accounted_rows = (
        len(matches) + len(rejected_matches) + len(manual_review_matches)
    )
    partition_status = (
        "complete" if accounted_rows == len(related_matches) else "INCOMPLETE"
    )
    lines = [
        "# Source Audit",
        "",
        f"- Generated at UTC: {datetime.now(timezone.utc).isoformat()}",
        f"- All discovered vacancies: {len(jobs)}",
        f"- Source-agent rows before deduplication: {rows_before_dedupe}",
        f"- Duplicate/syndicated rows consolidated: {consolidated_rows}",
        f"- Related vacancies before final LLM review: {len(related_matches)}",
        f"- Final vacancies: {len(matches)}",
        f"- Rejected during final eligibility review: {len(rejected_matches)}",
        f"- Vacancies requiring manual review: {len(manual_review_matches)}",
        f"- Final output partition: {partition_status} ({accounted_rows} of {len(related_matches)} related rows)",
        f"- Output rows with a recorded LLM decision: {decisioned_rows}",
        "",
        "A connector returning zero rows does not prove that its website has no vacancies. "
        "Only rows actually retrieved during this run are counted.",
        "",
        "## Connector Status Definitions",
        "",
        "- `completed_with_results`: the connector ran and returned one or more rows.",
        "- `completed_inventory_no_role_candidates`: a complete current inventory was loaded, but the deterministic role-keyword gate returned no candidates.",
        "- `connector_empty_unverified`: the connector ran but returned no rows; website inventory was not verified empty.",
        "- `skipped`: the connector did not run, with the reason recorded below.",
        "- `failed`: the connector attempted to run but raised an error.",
        "",
    ]
    if source_traces:
        lines.extend(
            [
                "## Run Outcome",
                "",
                f"- Completed with results: {status_counts['completed_with_results']}",
                f"- Complete inventory, no role candidates: {status_counts['completed_inventory_no_role_candidates']}",
                f"- Connector empty, inventory unverified: {status_counts['connector_empty_unverified']}",
                f"- Skipped: {status_counts['skipped']}",
                f"- Failed: {status_counts['failed']}",
                "",
                "| Source | Connector | Status | Full inventory rows | Role-candidate rows returned | Unique discovered | Related | Final | Final rejected | Manual review | Note |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for trace in source_traces:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(trace.source),
                        _markdown_cell(trace.connector),
                        f"`{trace.status}`",
                        "" if trace.inventory_total is None else str(trace.inventory_total),
                        str(trace.discovered),
                        str(discovered_by_source[trace.source]),
                        str(related_by_source[trace.source]),
                        str(final_by_source[trace.source]),
                        str(rejected_by_source[trace.source]),
                        str(manual_by_source[trace.source]),
                        _markdown_cell(trace.note),
                    ]
                )
                + " |"
            )
    else:
        lines.extend(
            [
                "## Run Outcome",
                "",
                "Structured source traces were not supplied for this run, so per-connector "
                "completion or emptiness cannot be asserted.",
            ]
        )
        if provider_notes:
            lines.extend(["", "### Legacy connector messages", ""])
            lines.extend(f"- {_markdown_cell(note)}" for note in provider_notes)

    lines.extend(
        [
            "",
            "## Interpretation Boundary",
            "",
            "Search-engine counts can include duplicates, cached pages, expired roles, "
            "global remote roles, and pages requiring login or JavaScript. This audit "
            "describes connector execution and retrieved records, not a guarantee that "
            "every vacancy on every website was captured.",
            "",
            "## Implementation Inventory",
            "",
            "The entries below describe configured or planned integrations. They are not "
            "evidence that a connector ran successfully in this report.",
            "",
        ]
    )
    for site, status, notes in SRI_LANKA_SOURCE_AUDIT:
        lines.append(f"- {site}: {status}. {notes}")
    return "\n".join(lines).strip() + "\n"


def _markdown_cell(value: object) -> str:
    return str(value or "").replace("|", r"\|").replace("\r", " ").replace("\n", " ")
