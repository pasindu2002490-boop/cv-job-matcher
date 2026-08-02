from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import logging
from pathlib import Path

from .agent_graph import AgentGraphOptions, VerticalJobAgentGraph
from .contact_finder import enrich_company_contacts
from .country import normalize_country
from .cv_parser import parse_cv, read_cv
from .cv_writer import build_tailored_cv
from .llm_filter import apply_llm_filter
from .matcher import (
    filter_country_compatible,
    filter_experience_compatible,
    filter_fresh_jobs,
    rank_jobs,
)
from .models import MatchResult
from .report import write_outputs

logger = logging.getLogger(__name__)


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
    llm_batch_size: int = 5
    limit_per_source: int = 200
    minimum_score: float = 40.0
    find_contacts: bool = False
    contact_limit_companies: int = 50
    contact_results_per_query: int = 3
    include_public_personal_emails: bool = False
    vertical_agent_graph: bool = True


@dataclass(frozen=True)
class RunSummary:
    country: str
    candidate_name: str
    jobs_fetched: int
    matches_written: int
    contact_leads_written: int
    output_dir: Path
    related_jobs: int = 0
    rejected_jobs: int = 0
    manual_review_jobs: int = 0


def run_match(options: RunOptions) -> RunSummary:
    """Run the matcher once and write the same artifacts used by the CLI."""
    logger.info("Reading and parsing CV: %s", options.cv_path.name)
    text = read_cv(options.cv_path)
    profile = parse_cv(text)
    profile = replace(
        profile,
        target_position=options.position.strip(),
        experience_years=options.experience_years,
    )
    country = normalize_country(options.country)
    logger.info(
        "Candidate request: position=%s, experience=%s years, country=%s, skills=%d",
        profile.target_position,
        profile.experience_years,
        country,
        len(profile.skills),
    )
    logger.info("Starting live job discovery")
    graph_filtered = options.vertical_agent_graph
    source_traces = []
    if graph_filtered:
        graph = VerticalJobAgentGraph(AgentGraphOptions(
            limit_per_source=options.limit_per_source,
            include_remote_global=options.include_remote_global,
            web_discovery=options.web_discovery,
            minimum_score=options.minimum_score,
            llm_enabled=options.llm_filter,
            llm_model=options.llm_model,
            llm_provider=options.llm_provider,
            llm_strict=options.llm_strict,
            llm_batch_size=options.llm_batch_size,
        ))
        graph_state = graph.run(profile, country)
        jobs, provider_notes = graph_state.jobs, graph_state.notes
        source_traces = graph_state.traces
    else:
        from .job_sources import search_all
        jobs, provider_notes = search_all(
            profile,
            country,
            options.limit_per_source,
            include_remote_global=options.include_remote_global,
            web_discovery=options.web_discovery,
        )
    discovered_jobs = list(jobs)
    logger.info("Job discovery complete: %d unique raw jobs", len(discovered_jobs))
    jobs, country_rejections = filter_country_compatible(
        jobs, country, allow_global_remote=options.include_remote_global
    )
    logger.info(
        "Country filter retained %d jobs and rejected %d jobs outside %s",
        len(jobs),
        country_rejections,
        country,
    )
    if country_rejections:
        provider_notes.append(
            f"Country verification: rejected {country_rejections} job(s) outside {country}"
        )
    jobs, freshness_rejections = filter_fresh_jobs(jobs, max_age_days=30)
    logger.info(
        "Freshness filter retained %d jobs and rejected %d stale or unverifiable jobs",
        len(jobs),
        freshness_rejections,
    )
    if freshness_rejections:
        provider_notes.append(
            f"Freshness verification: rejected {freshness_rejections} stale or unverifiable job(s)"
        )
    logger.info("Running deterministic CV and position matching")
    # Source nodes already apply the broad requested-role keyword gate. Keep
    # every surviving source row for the single strict LLM, even if heuristic
    # cross-role penalties make its ranking score negative.
    matches = rank_jobs(
        profile,
        jobs,
        float("-inf") if graph_filtered else options.minimum_score,
    )
    related_matches = list(matches)
    logger.info("Keyword matcher retained %d related jobs for final LLM review", len(matches))
    rejected_matches = []
    manual_review_matches = []
    completed_llm_reviews = []
    if options.llm_filter and options.llm_strict:
        # Persist the expensive crawl and the complete pre-LLM candidate set
        # before contacting either model provider. A later provider outage must
        # not erase the evidence collected from the source agents.
        checkpoint_notes = [
            *provider_notes,
            (
                "Pre-LLM checkpoint: discovery and all related vacancy rows "
                "were persisted before final model review"
            ),
        ]
        logger.info("Writing pre-LLM discovery checkpoint to %s", options.out_dir)
        write_outputs(
            options.out_dir,
            profile,
            country,
            discovered_jobs,
            [],
            checkpoint_notes,
            build_tailored_cv(profile, [], country),
            contact_leads=[],
            related_matches=related_matches,
            rejected_matches=[],
            manual_review_matches=[],
            source_traces=source_traces,
        )
    try:
        matches, llm_note = apply_llm_filter(
            profile,
            matches,
            enabled=options.llm_filter,
            model=options.llm_model,
            limit=options.llm_limit,
            provider=options.llm_provider,
            strict=options.llm_strict,
            batch_size=options.llm_batch_size,
            country=country,
            allow_global_remote=options.include_remote_global,
            rejected_audit=rejected_matches,
            manual_review_audit=manual_review_matches,
            completed_audit=completed_llm_reviews,
            checkpoint_path=options.out_dir / "llm_review_checkpoint.jsonl",
        )
    except RuntimeError:
        # Preserve user-readable partial outputs as well as the fsynced JSONL
        # progress file. A retry with the same candidate/output directory will
        # restore these decisions and continue only unfinished vacancies.
        partial_matches = [
            match
            for match in completed_llm_reviews
            if match.llm_decision == "keep"
        ]
        partial_rejected = [
            match
            for match in completed_llm_reviews
            if match.llm_decision in {"reject", "maybe"}
        ]
        partial_manual = [
            match
            for match in completed_llm_reviews
            if match.llm_decision == "review_failed"
        ]
        failure_notes = [
            *provider_notes,
            (
                "Final LLM review stopped after "
                f"{len(completed_llm_reviews)} of {len(related_matches)} row(s); "
                "completed decisions were preserved in the internal review checkpoint"
            ),
        ]
        logger.warning(
            "Final LLM review stopped after %d/%d rows; preserving partial outputs",
            len(completed_llm_reviews),
            len(related_matches),
        )
        write_outputs(
            options.out_dir,
            profile,
            country,
            discovered_jobs,
            partial_matches,
            failure_notes,
            build_tailored_cv(profile, partial_matches, country),
            contact_leads=[],
            related_matches=related_matches,
            rejected_matches=partial_rejected,
            manual_review_matches=partial_manual,
            source_traces=source_traces,
        )
        raise
    logger.info("Final LLM filter retained %d jobs", len(matches))
    if llm_note:
        provider_notes.append(llm_note)
    pre_experience_matches = list(matches)
    matches, experience_rejections = filter_experience_compatible(profile, matches)
    kept_after_experience = {id(match) for match in matches}
    rejected_matches.extend(
        replace(
            match,
            llm_decision="reject",
            llm_reason=(
                (match.llm_reason + "; ") if match.llm_reason else ""
            ) + "Rejected by final deterministic experience/seniority safety check",
        )
        for match in pre_experience_matches
        if id(match) not in kept_after_experience
    )
    logger.info(
        "Final experience safety check retained %d jobs and rejected %d seniority-mismatched jobs",
        len(matches),
        experience_rejections,
    )
    if experience_rejections:
        provider_notes.append(
            f"Final experience check: rejected {experience_rejections} seniority-mismatched job(s)"
        )
    if options.llm_filter and options.llm_strict:
        expected = Counter(_match_audit_key(match) for match in related_matches)
        audited = Counter(
            _match_audit_key(match)
            for match in [
                *matches,
                *rejected_matches,
                *manual_review_matches,
            ]
        )
        if audited != expected:
            missing = sum((expected - audited).values())
            duplicated = sum((audited - expected).values())
            raise RuntimeError(
                "Strict LLM audit invariant failed: "
                f"{missing} related vacancy row(s) unaccounted for and "
                f"{duplicated} duplicate audit row(s)"
            )
        provider_notes.append(
            f"Final LLM audit: all {len(related_matches)} related vacancy row(s) "
            "accounted for, including explicit manual-review rows"
        )

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
    logger.info("Writing CSV reports to %s", options.out_dir)
    write_outputs(
        options.out_dir,
        profile,
        country,
        discovered_jobs,
        matches,
        provider_notes,
        tailored_cv,
        contact_leads=contact_leads,
        related_matches=related_matches,
        rejected_matches=rejected_matches,
        manual_review_matches=manual_review_matches,
        source_traces=source_traces,
    )
    logger.info("Report generation complete")
    return RunSummary(
        country=country,
        candidate_name=profile.name,
        jobs_fetched=len(discovered_jobs),
        matches_written=len(matches),
        contact_leads_written=len(contact_leads),
        output_dir=options.out_dir.resolve(),
        related_jobs=len(related_matches),
        rejected_jobs=len(rejected_matches),
        manual_review_jobs=len(manual_review_matches),
    )


def _match_audit_key(match: MatchResult) -> tuple[str, str, str, str, str]:
    job = match.job
    return (
        job.source,
        job.source_id,
        job.url,
        job.title,
        job.company,
    )
