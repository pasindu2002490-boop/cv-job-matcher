import csv
from dataclasses import replace
from pathlib import Path

import pytest

from cv_job_matcher.agent_graph import AgentGraphState
from cv_job_matcher.models import CandidateProfile, Job
from cv_job_matcher.runner import RunOptions, run_match


def test_strict_runner_fails_if_any_related_row_is_not_accounted_for(
    monkeypatch,
    tmp_path: Path,
):
    profile = CandidateProfile(
        raw_text="Software engineer with Python",
        skills=("python",),
        likely_titles=("software engineer",),
        target_position="Software Engineer",
        experience_years=2,
    )
    jobs = [
        Job(
            "test",
            str(index),
            "Software Engineer",
            "Example",
            "Colombo, Sri Lanka",
            "sri lanka",
            f"https://example.lk/jobs/{index}",
            "Python software engineering role.",
            published_at="2026-07-28",
        )
        for index in range(2)
    ]

    class FakeGraph:
        def __init__(self, options):
            pass

        def run(self, profile, country):
            return AgentGraphState(profile, country, jobs=list(jobs))

    def incomplete_llm(profile, matches, **kwargs):
        return matches[:1], "LLM silently omitted one row"

    monkeypatch.setattr("cv_job_matcher.runner.read_cv", lambda path: profile.raw_text)
    monkeypatch.setattr("cv_job_matcher.runner.parse_cv", lambda text: profile)
    monkeypatch.setattr("cv_job_matcher.runner.VerticalJobAgentGraph", FakeGraph)
    monkeypatch.setattr("cv_job_matcher.runner.apply_llm_filter", incomplete_llm)

    with pytest.raises(RuntimeError, match="1 related vacancy row"):
        run_match(
            RunOptions(
                cv_path=tmp_path / "cv.txt",
                country="Sri Lanka",
                position="Software Engineer",
                experience_years=2,
                out_dir=tmp_path / "out",
                llm_filter=True,
                llm_strict=True,
            )
        )


def test_graph_mode_passes_negative_heuristic_role_candidate_to_final_llm(
    monkeypatch,
    tmp_path: Path,
):
    profile = CandidateProfile(
        raw_text="AI Engineer with machine learning experience",
        skills=("machine learning",),
        likely_titles=("ai engineer",),
        target_position="DevOps Engineer",
        experience_years=2,
    )
    job = Job(
        "test",
        "devops",
        "DevOps Engineer",
        "Example",
        "Colombo, Sri Lanka",
        "sri lanka",
        "https://example.lk/jobs/devops",
        "Operate production infrastructure.",
        published_at="2026-07-28",
    )

    class FakeGraph:
        def __init__(self, options):
            pass

        def run(self, profile, country):
            return AgentGraphState(profile, country, jobs=[job])

    reviewed = []
    written = {}

    def accepting_llm(profile, matches, **kwargs):
        reviewed.extend(matches)
        return [
            replace(match, llm_decision="keep", llm_reason="target role")
            for match in matches
        ], "reviewed"

    def capture_outputs(*args, **kwargs):
        written["related"] = kwargs["related_matches"]

    monkeypatch.setattr("cv_job_matcher.runner.read_cv", lambda path: profile.raw_text)
    monkeypatch.setattr("cv_job_matcher.runner.parse_cv", lambda text: profile)
    monkeypatch.setattr("cv_job_matcher.runner.VerticalJobAgentGraph", FakeGraph)
    monkeypatch.setattr("cv_job_matcher.runner.apply_llm_filter", accepting_llm)
    monkeypatch.setattr("cv_job_matcher.runner.write_outputs", capture_outputs)

    summary = run_match(
        RunOptions(
            cv_path=tmp_path / "cv.txt",
            country="Sri Lanka",
            position="DevOps Engineer",
            experience_years=2,
            out_dir=tmp_path / "out",
            llm_filter=True,
            llm_strict=True,
        )
    )

    assert reviewed[0].score < 0
    assert written["related"] == reviewed
    assert summary.related_jobs == 1
    assert summary.matches_written == 1


def test_strict_llm_failure_preserves_discovered_and_related_csvs(
    monkeypatch,
    tmp_path: Path,
):
    profile = CandidateProfile(
        raw_text="Software engineer with Python",
        skills=("python",),
        likely_titles=("software engineer",),
        target_position="Software Engineer",
        experience_years=2,
    )
    job = Job(
        "test",
        "persisted-job",
        "Software Engineer",
        "Example",
        "Colombo, Sri Lanka",
        "sri lanka",
        "https://example.lk/jobs/persisted-job",
        "Python software engineering role.",
        published_at="2026-07-28",
    )

    class FakeGraph:
        def __init__(self, options):
            pass

        def run(self, profile, country):
            return AgentGraphState(profile, country, jobs=[job])

    monkeypatch.setattr("cv_job_matcher.runner.read_cv", lambda path: profile.raw_text)
    monkeypatch.setattr("cv_job_matcher.runner.parse_cv", lambda text: profile)
    monkeypatch.setattr("cv_job_matcher.runner.VerticalJobAgentGraph", FakeGraph)
    monkeypatch.setattr(
        "cv_job_matcher.runner.apply_llm_filter",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("strict provider failure")
        ),
    )
    output_dir = tmp_path / "out"

    with pytest.raises(RuntimeError, match="strict provider failure"):
        run_match(
            RunOptions(
                cv_path=tmp_path / "cv.txt",
                country="Sri Lanka",
                position="Software Engineer",
                experience_years=2,
                out_dir=output_dir,
                llm_filter=True,
                llm_strict=True,
            )
        )

    with (output_dir / "all_discovered_jobs.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        discovered_rows = list(csv.DictReader(handle))
    with (output_dir / "related_vacancies.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        related_rows = list(csv.DictReader(handle))

    assert [row["title"] for row in discovered_rows] == ["Software Engineer"]
    assert [row["title"] for row in related_rows] == ["Software Engineer"]


def test_runner_preserves_partial_decisions_when_a_later_review_fails(
    monkeypatch,
    tmp_path: Path,
):
    profile = CandidateProfile(
        raw_text="Software engineer with Python",
        skills=("python",),
        likely_titles=("software engineer",),
        target_position="Software Engineer",
        experience_years=2,
    )
    jobs = [
        Job(
            "test",
            str(index),
            "Software Engineer",
            "Example",
            "Colombo, Sri Lanka",
            "sri lanka",
            f"https://example.lk/jobs/{index}",
            "Python software engineering role.",
            published_at="2026-07-28",
        )
        for index in range(2)
    ]

    class FakeGraph:
        def __init__(self, options):
            pass

        def run(self, profile, country):
            return AgentGraphState(profile, country, jobs=list(jobs))

    def interrupted_llm(profile, matches, **kwargs):
        audited = replace(
            matches[0],
            llm_decision="keep",
            llm_reason="completed before outage",
            llm_provider="Groq",
            llm_model="model",
        )
        kwargs["completed_audit"].append(audited)
        raise RuntimeError("later provider failure")

    monkeypatch.setattr("cv_job_matcher.runner.read_cv", lambda path: profile.raw_text)
    monkeypatch.setattr("cv_job_matcher.runner.parse_cv", lambda text: profile)
    monkeypatch.setattr("cv_job_matcher.runner.VerticalJobAgentGraph", FakeGraph)
    monkeypatch.setattr("cv_job_matcher.runner.apply_llm_filter", interrupted_llm)
    output_dir = tmp_path / "out"

    with pytest.raises(RuntimeError, match="later provider failure"):
        run_match(
            RunOptions(
                cv_path=tmp_path / "cv.txt",
                country="Sri Lanka",
                position="Software Engineer",
                experience_years=2,
                out_dir=output_dir,
                llm_filter=True,
                llm_strict=True,
            )
        )

    with (output_dir / "job_matches.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        kept_rows = list(csv.DictReader(handle))
    audit = (output_dir / "source_audit.md").read_text(encoding="utf-8")

    assert [row["apply_url"] for row in kept_rows] == [
        "https://example.lk/jobs/0"
    ]
    assert "- Final output partition: INCOMPLETE (1 of 2 related rows)" in audit
