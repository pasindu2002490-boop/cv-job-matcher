from __future__ import annotations

from dataclasses import replace

from ..llm_filter import review_evidence_hash, review_identity
from ..models import MatchResult
from .repository import JobReviewInput, PersistenceRepository


class DatabaseReviewCheckpointStore:
    """Durable per-task review checkpoint store backed by the repository."""

    def __init__(self, repository: PersistenceRepository, task_id: str) -> None:
        self._repository = repository
        self._task_id = task_id

    def load(self, context: str, matches: list[MatchResult]) -> dict[str, MatchResult]:
        expected = {review_identity(match): match for match in matches}
        restored: dict[str, MatchResult] = {}
        for review in self._repository.list_job_reviews(self._task_id):
            if review.context_hash != context:
                continue
            match = expected.get(review.job_id)
            if match is None:
                continue
            if review.evidence_hash != review_evidence_hash(match):
                continue
            if review.decision not in {"accepted", "rejected", "review_failed"}:
                continue
            restored[review_identity(match)] = replace(
                match,
                score=review.score if review.score is not None else match.score,
                llm_decision={
                    "accepted": "keep",
                    "rejected": "reject",
                    "review_failed": "review_failed",
                }[review.decision],
                llm_reason=review.reason,
                llm_provider=review.provider,
                llm_model=review.model,
            )
        return restored

    def append(self, context: str, audited: MatchResult, evidence_match: MatchResult) -> None:
        decision = audited.llm_decision.strip().lower()
        if decision == "keep":
            stored_decision = "accepted"
        elif decision in {"reject", "maybe"}:
            stored_decision = "rejected"
        else:
            stored_decision = "review_failed"

        self._repository.upsert_job_review(
            JobReviewInput(
                task_id=self._task_id,
                job_id=evidence_match.job.id,
                context_hash=context,
                evidence_hash=review_evidence_hash(evidence_match),
                decision=stored_decision,
                score=audited.score,
                reason=audited.llm_reason,
                provider=audited.llm_provider,
                model=audited.llm_model,
                matched_skills=audited.matched_skills,
                concerns=audited.concerns,
                raw_response={
                    "review_id": review_identity(evidence_match),
                    "decision": audited.llm_decision,
                },
                review_id=review_identity(evidence_match),
            )
        )
