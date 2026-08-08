import csv
from pathlib import Path

from cv_job_matcher.agent_graph import SourceAgentTrace
from cv_job_matcher.models import CandidateProfile, Job, MatchResult
from cv_job_matcher.report import write_outputs


def _job(source: str, source_id: str, title: str) -> Job:
    return Job(
        source=source,
        source_id=source_id,
        title=title,
        company="Example",
        location="Colombo, Sri Lanka",
        country_hint="sri lanka",
        url=f"https://example.lk/jobs/{source_id}",
        description=f"{title} vacancy",
    )


def test_outputs_preserve_run_coverage_and_llm_rejections(tmp_path: Path):
    profile = CandidateProfile(raw_text="CV", target_position="DevOps Engineer")
    kept = MatchResult(
        _job("Working", "1", "DevOps Engineer"),
        90,
        ("devops",),
        ("devops engineer",),
        llm_decision="keep",
        llm_reason="Clear fit",
        llm_provider="Groq",
        llm_model="openai/gpt-oss-20b",
    )
    rejected = MatchResult(
        _job("Working", "2", "Senior DevOps Engineer"),
        40,
        ("devops",),
        ("devops engineer",),
        llm_decision="reject",
        llm_reason="Requires more experience",
        llm_provider="Ollama",
        llm_model="llama3.1:8b",
    )
    manual = MatchResult(
        _job("Working", "3", "DevOps Platform Engineer"),
        55,
        ("devops",),
        ("devops engineer",),
        llm_decision="review_failed",
        llm_reason="Manual review required: invalid structured output",
        llm_provider="Ollama",
        llm_model="llama3.1:8b",
    )
    traces = [
        SourceAgentTrace(
            "Working",
            "official-api/rss/html",
            "completed_with_results",
            discovered=3,
            eligible=3,
            inventory_total=500,
        ),
        SourceAgentTrace(
            "Empty",
            "crawl4ai/html",
            "connector_empty_unverified",
            note="connector returned no rows; website inventory not verified empty",
        ),
        SourceAgentTrace(
            "Disabled",
            "official-api/rss/html",
            "skipped",
            note="credentials not configured",
        ),
        SourceAgentTrace(
            "Broken",
            "official-api/rss/html",
            "failed",
            note="portal timeout",
        ),
    ]

    write_outputs(
        tmp_path,
        profile,
        "sri lanka",
        [kept.job, rejected.job, manual.job],
        [kept],
        [],
        "Tailored CV",
        related_matches=[kept, rejected, manual],
        rejected_matches=[rejected],
        manual_review_matches=[manual],
        source_traces=traces,
    )

    with (tmp_path / "source_coverage.csv").open(newline="", encoding="utf-8") as handle:
        coverage = list(csv.DictReader(handle))
    assert coverage[0]["status"] == "completed_with_results"
    assert coverage[0]["full_inventory_rows"] == "500"
    assert coverage[0]["unique_discovered"] == "3"
    assert coverage[0]["related_before_llm"] == "3"
    assert coverage[0]["final_vacancies"] == "1"
    assert coverage[0]["final_rejected"] == "1"
    assert coverage[0]["final_manual_review"] == "1"
    assert coverage[1]["status"] == "connector_empty_unverified"

    with (tmp_path / "rejected_vacancies.csv").open(newline="", encoding="utf-8") as handle:
        rejected_rows = list(csv.DictReader(handle))
    assert rejected_rows[0]["llm_decision"] == "reject"
    assert rejected_rows[0]["llm_reason"] == "Requires more experience"
    assert rejected_rows[0]["llm_provider"] == "Ollama"
    assert rejected_rows[0]["llm_model"] == "llama3.1:8b"

    with (tmp_path / "manual_review_vacancies.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        manual_rows = list(csv.DictReader(handle))
    assert manual_rows[0]["llm_decision"] == "review_failed"
    assert manual_rows[0]["llm_reason"].startswith("Manual review required:")

    with (tmp_path / "job_matches.csv").open(newline="", encoding="utf-8") as handle:
        kept_rows = list(csv.DictReader(handle))
    assert list(kept_rows[0]) == [
        "match_score", "title", "company", "location", "source",
        "published_at", "fetched_at_utc", "detail_page_verified",
        "matched_skills", "concerns", "apply_url",
    ]

    audit = (tmp_path / "source_audit.md").read_text(encoding="utf-8")
    assert "- All discovered vacancies: 3" in audit
    assert "- Source-agent rows before deduplication: 3" in audit
    assert "- Duplicate/syndicated rows consolidated: 0" in audit
    assert "- Related vacancies before final LLM review: 3" in audit
    assert "- Final vacancies: 1" in audit
    assert "- Rejected during final eligibility review: 1" in audit
    assert "- Vacancies requiring manual review: 1" in audit
    assert "- Final output partition: complete (3 of 3 related rows)" in audit
    assert "- Output rows with a recorded LLM decision: 3" in audit
    assert "`connector_empty_unverified`" in audit


def test_source_coverage_attributes_each_pipeline_loss_to_its_source(
    tmp_path: Path,
):
    profile = CandidateProfile(raw_text="CV", target_position="DevOps Engineer")
    source_a_kept = MatchResult(
        _job("Source A", "a-kept", "DevOps Engineer"),
        90,
        ("devops",),
        ("devops engineer",),
        llm_decision="keep",
    )
    source_a_filtered_before_llm = _job(
        "Source A",
        "a-unrelated",
        "Accounts Executive",
    )
    source_b_rejected = MatchResult(
        _job("Source B", "b-rejected", "Senior DevOps Engineer"),
        40,
        ("devops",),
        ("devops engineer",),
        llm_decision="reject",
        llm_reason="Requires more experience",
    )
    traces = [
        SourceAgentTrace(
            "Source A",
            "html",
            "completed_with_results",
            discovered=2,
        ),
        SourceAgentTrace(
            "Source B",
            "api",
            "completed_with_results",
            discovered=1,
        ),
    ]

    write_outputs(
        tmp_path,
        profile,
        "sri lanka",
        [
            source_a_kept.job,
            source_a_filtered_before_llm,
            source_b_rejected.job,
        ],
        [source_a_kept],
        [],
        "Tailored CV",
        related_matches=[source_a_kept, source_b_rejected],
        rejected_matches=[source_b_rejected],
        source_traces=traces,
    )

    with (tmp_path / "source_coverage.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["source"]: row for row in csv.DictReader(handle)}

    assert {
        key: rows["Source A"][key]
        for key in (
            "role_candidate_rows_returned",
            "unique_discovered",
            "related_before_llm",
            "final_vacancies",
            "final_rejected",
        )
    } == {
        "role_candidate_rows_returned": "2",
        "unique_discovered": "2",
        "related_before_llm": "1",
        "final_vacancies": "1",
        "final_rejected": "0",
    }
    assert {
        key: rows["Source B"][key]
        for key in (
            "role_candidate_rows_returned",
            "unique_discovered",
            "related_before_llm",
            "final_vacancies",
            "final_rejected",
        )
    } == {
        "role_candidate_rows_returned": "1",
        "unique_discovered": "1",
        "related_before_llm": "1",
        "final_vacancies": "0",
        "final_rejected": "1",
    }
