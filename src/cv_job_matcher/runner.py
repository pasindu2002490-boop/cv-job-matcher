from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .contact_finder import enrich_company_contacts
from .country import normalize_country
from .cv_parser import parse_cv, read_cv
from .cv_writer import build_tailored_cv
from .job_sources import search_all
from .llm_filter import apply_llm_filter
from .matcher import filter_experience_compatible, rank_jobs
from .report import write_outputs


@dataclass(frozen=True)
class RunOptions:
    cv_path: Path
    country: str
    position: str = ""
    experience_years: float | None = None
    out_dir: Path = Path("out")
    include_remote_global: bool = False
    web_discovery: bool = False
    llm_filter: bool = False
    llm_model: str = "gpt-4.1-mini"
    llm_provider: str = "auto"
    llm_limit: int = 80
    llm_strict: bool = False
    llm_batch_size: int = 10
    limit_per_source: int = 50
    minimum_score: float = 40.0
    find_contacts: bool = False
    contact_limit_companies: int = 50
    contact_results_per_query: int = 3
    include_public_personal_emails: bool = False


@dataclass(frozen=True)
class RunSummary:
    country: str
    candidate_name: str
    jobs_fetched: int
    matches_written: int
    contact_leads_written: int
    output_dir: Path


def run_match(options: RunOptions) -> RunSummary:
    """Run the matcher once and write the same artifacts used by the CLI."""
    text = read_cv(options.cv_path)
    profile = parse_cv(text)
    profile = replace(
        profile,
        target_position=options.position.strip(),
        experience_years=options.experience_years,
    )
    country = normalize_country(options.country)
    jobs, provider_notes = search_all(
        profile,
        country,
        options.limit_per_source,
        include_remote_global=options.include_remote_global,
        web_discovery=options.web_discovery,
    )
    matches = rank_jobs(profile, jobs, options.minimum_score)
    matches, experience_rejections = filter_experience_compatible(profile, matches)
    if experience_rejections:
        provider_notes.append(
            f"Experience filter: rejected {experience_rejections} over-senior job(s)"
        )
    matches, llm_note = apply_llm_filter(
        profile,
        matches,
        enabled=options.llm_filter,
        model=options.llm_model,
        limit=options.llm_limit,
        provider=options.llm_provider,
        strict=options.llm_strict,
        batch_size=options.llm_batch_size,
    )
    if llm_note:
        provider_notes.append(llm_note)

    contact_leads = []
    if options.find_contacts:
        contact_leads, contact_notes = enrich_company_contacts(
            matches,
            country,
            limit_companies=options.contact_limit_companies,
            results_per_query=options.contact_results_per_query,
            include_public_personal_emails=options.include_public_personal_emails,
        )
        provider_notes.extend(contact_notes)

    tailored_cv = build_tailored_cv(profile, matches, country)
    write_outputs(
        options.out_dir,
        profile,
        country,
        jobs,
        matches,
        provider_notes,
        tailored_cv,
        contact_leads=contact_leads,
    )
    return RunSummary(
        country=country,
        candidate_name=profile.name,
        jobs_fetched=len(jobs),
        matches_written=len(matches),
        contact_leads_written=len(contact_leads),
        output_dir=options.out_dir.resolve(),
    )
