from __future__ import annotations

from dataclasses import dataclass, replace
import os

from .matcher import required_experience_years
from .models import CandidateProfile, MatchResult


@dataclass(frozen=True)
class ConfidenceRoute:
    accepted: tuple[MatchResult, ...]
    rejected: tuple[MatchResult, ...]
    ambiguous: tuple[MatchResult, ...]


def route_by_confidence(
    profile: CandidateProfile,
    matches: list[MatchResult],
) -> ConfidenceRoute:
    """Route only evidence-rich, unambiguous rows around the expensive LLM gate."""
    accept_score = _bounded_float("DETERMINISTIC_ACCEPT_SCORE", 85.0, 60.0, 100.0)
    min_skills = int(_bounded_float("DETERMINISTIC_ACCEPT_MIN_SKILLS", 2, 1, 20))
    min_description = int(
        _bounded_float("DETERMINISTIC_ACCEPT_MIN_DESCRIPTION_CHARS", 200, 0, 10000)
    )
    accepted: list[MatchResult] = []
    rejected: list[MatchResult] = []
    ambiguous: list[MatchResult] = []

    for match in matches:
        required_years = required_experience_years(match.job)
        if (
            profile.experience_years is not None
            and required_years is not None
            and required_years > profile.experience_years
        ):
            rejected.append(
                replace(
                    match,
                    llm_decision="reject",
                    llm_reason="Deterministic rejection: required experience exceeds candidate experience",
                    llm_provider="deterministic-confidence-router",
                    llm_model="v1",
                )
            )
            continue

        if (
            match.score >= accept_score
            and len(match.matched_skills) >= min_skills
            and len(match.job.description.strip()) >= min_description
            and not match.concerns
        ):
            accepted.append(
                replace(
                    match,
                    llm_decision="keep",
                    llm_reason="Deterministic acceptance: high-confidence title and CV evidence",
                    llm_provider="deterministic-confidence-router",
                    llm_model="v1",
                )
            )
            continue

        ambiguous.append(match)

    return ConfidenceRoute(tuple(accepted), tuple(rejected), tuple(ambiguous))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
