from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from typing import Mapping, Sequence

from .agent_graph import SourceAgentTrace
from .country import normalize_country
from .cv_parser import parse_cv, read_cv
from .cv_writer import build_tailored_cv
from .confidence_router import route_by_confidence
from .job_sources import job_title_matches_profile
from .llm_filter import ReviewCheckpointStore, apply_llm_filter
from .matcher import (
    filter_country_compatible,
    filter_experience_compatible,
    filter_fresh_jobs,
    rank_jobs,
)
from .models import Job, MatchResult
from .report import write_outputs
from .runner import RunSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SharedInventoryMatchOptions:
    cv_path: Path
    jobs: Sequence[Job]
    country: str
    position: str
    experience_years: float | None
    out_dir: Path
    include_remote_global: bool = False
    llm_model: str = "openai/gpt-oss-20b"
    llm_provider: str = "groq"
    llm_batch_size: int = 5
    source_metadata: Mapping[str, str] | None = None
    source_traces: Sequence[SourceAgentTrace] | None = None


def run_shared_inventory_match(
    options: SharedInventoryMatchOptions,
    *,
    checkpoint_store: ReviewCheckpointStore,
) -> RunSummary:
    """Match one immutable inventory snapshot without launching any crawler."""
    logger.info("Reading CV for persistent inventory match: %s", options.cv_path.name)
    profile = replace(
        parse_cv(read_cv(options.cv_path)),
        target_position=options.position.strip(),
        experience_years=options.experience_years,
    )
    if not profile.target_position:
        raise ValueError("A target position is required")
    country = normalize_country(options.country)
    discovered_jobs = list(options.jobs)
    source_traces = list(options.source_traces or _snapshot_source_traces(
        discovered_jobs, options.source_metadata or {}
    ))
    provider_notes = [
        (
            "Shared inventory: the task uses an immutable database snapshot; "
            "no source website is crawled for this user"
        )
    ]

    jobs, country_rejections = filter_country_compatible(
        discovered_jobs,
        country,
        allow_global_remote=options.include_remote_global,
    )
    jobs, freshness_rejections = filter_fresh_jobs(jobs, max_age_days=30)
    role_jobs = [
        job for job in jobs if job_title_matches_profile(profile, job.title)
    ]
    related_matches = rank_jobs(profile, role_jobs, float("-inf"))
    provider_notes.extend(
        [
            (
                f"Country verification: retained {len(jobs)} after "
                f"{country_rejections} country rejection(s)"
            ),
            (
                f"Freshness verification: rejected {freshness_rejections} "
                "stale or unverifiable row(s)"
            ),
            (
                f"Deterministic role-keyword gate: {len(related_matches)} "
                f"of {len(jobs)} active rows matched the requested position"
            ),
        ]
    )

    # This is a user-readable checkpoint of the exact snapshot and pre-LLM
    # candidate set. Per-vacancy decisions are checkpointed synchronously by
    # the supplied PostgreSQL store below.
    write_outputs(
        options.out_dir,
        profile,
        country,
        discovered_jobs,
        [],
        provider_notes,
        build_tailored_cv(profile, [], country),
        related_matches=related_matches,
        rejected_matches=[],
        manual_review_matches=[],
        source_traces=source_traces,
    )

    route = route_by_confidence(profile, related_matches)
    matches = list(route.accepted)
    rejected_matches: list[MatchResult] = list(route.rejected)
    manual_review_matches: list[MatchResult] = []
    completed_reviews: list[MatchResult] = []
    provider_notes.append(
        "Confidence router: "
        f"{len(route.accepted)} accepted deterministically, "
        f"{len(route.rejected)} rejected deterministically, and "
        f"{len(route.ambiguous)} sent to the LLM"
    )
    try:
        if route.ambiguous:
            llm_matches, llm_note = apply_llm_filter(
                profile,
                list(route.ambiguous),
                enabled=True,
                model=options.llm_model,
                limit=len(route.ambiguous),
                provider=options.llm_provider,
                strict=True,
                batch_size=options.llm_batch_size,
                country=country,
                allow_global_remote=options.include_remote_global,
                rejected_audit=rejected_matches,
                manual_review_audit=manual_review_matches,
                completed_audit=completed_reviews,
                checkpoint_store=checkpoint_store,
            )
            matches.extend(llm_matches)
        else:
            llm_note = "LLM review skipped: every related vacancy had a high-confidence deterministic route"
    except Exception:
        partial_matches = [*route.accepted, *[
            match for match in completed_reviews if match.llm_decision == "keep"
        ]]
        partial_rejected = [
            *route.rejected,
            *[
            match
            for match in completed_reviews
            if match.llm_decision in {"reject", "maybe"}
            ],
        ]
        partial_manual = [
            match
            for match in completed_reviews
            if match.llm_decision == "review_failed"
        ]
        write_outputs(
            options.out_dir,
            profile,
            country,
            discovered_jobs,
            partial_matches,
            [
                *provider_notes,
                (
                    "Final review stopped after "
                    f"{len(completed_reviews)} of {len(related_matches)} rows; "
                    "completed decisions remain in PostgreSQL"
                ),
            ],
            build_tailored_cv(profile, partial_matches, country),
            related_matches=related_matches,
            rejected_matches=partial_rejected,
            manual_review_matches=partial_manual,
            source_traces=source_traces,
        )
        raise
    if llm_note:
        provider_notes.append(llm_note)

    before_experience = list(matches)
    matches, experience_rejections = filter_experience_compatible(profile, matches)
    kept_ids = {id(match) for match in matches}
    rejected_matches.extend(
        replace(
            match,
            llm_decision="reject",
            llm_reason=(
                ((match.llm_reason + "; ") if match.llm_reason else "")
                + "Rejected by final deterministic experience safety check"
            ),
        )
        for match in before_experience
        if id(match) not in kept_ids
    )
    if experience_rejections:
        provider_notes.append(
            f"Final experience check: rejected {experience_rejections} over-senior job(s)"
        )

    expected = Counter(_audit_key(match) for match in related_matches)
    audited = Counter(
        _audit_key(match)
        for match in [*matches, *rejected_matches, *manual_review_matches]
    )
    if expected != audited:
        raise RuntimeError(
            "Persistent LLM audit invariant failed: every related vacancy must "
            "appear exactly once in accepted, rejected, or review_failed"
        )

    write_outputs(
        options.out_dir,
        profile,
        country,
        discovered_jobs,
        matches,
        [
            *provider_notes,
            (
                f"Final LLM audit: all {len(related_matches)} related rows "
                "are accounted for"
            ),
        ],
        build_tailored_cv(profile, matches, country),
        related_matches=related_matches,
        rejected_matches=rejected_matches,
        manual_review_matches=manual_review_matches,
        source_traces=source_traces,
    )
    return RunSummary(
        country=country,
        candidate_name=profile.name,
        jobs_fetched=len(discovered_jobs),
        matches_written=len(matches),
        contact_leads_written=0,
        output_dir=options.out_dir.resolve(),
        related_jobs=len(related_matches),
        rejected_jobs=len(rejected_matches),
        manual_review_jobs=len(manual_review_matches),
    )


def _snapshot_source_traces(
    jobs: Sequence[Job],
    metadata: Mapping[str, str],
) -> list[SourceAgentTrace]:
    counts = Counter(job.source for job in jobs)
    return [
        SourceAgentTrace(
            source=source,
            connector="persistent_inventory",
            status="completed_with_results",
            discovered=count,
            eligible=count,
            inventory_total=count,
            note=metadata.get(source, "rows captured in the task inventory snapshot"),
        )
        for source, count in sorted(counts.items())
    ]


def _audit_key(match: MatchResult) -> tuple[str, str, str, str, str]:
    job = match.job
    return job.source, job.source_id, job.url, job.title, job.company
