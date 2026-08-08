from cv_job_matcher.cv_parser import parse_cv
from dataclasses import replace

import pytest

from cv_job_matcher.matcher import (
    filter_country_compatible,
    filter_experience_compatible,
    filter_fresh_jobs,
    rank_jobs,
    required_experience_years,
)
from cv_job_matcher.models import Job
from datetime import datetime, timezone


def test_parse_cv_extracts_core_fields():
    profile = parse_cv(
        """
        Jane Candidate
        jane@example.com
        Python developer with React, SQL, Docker, and AWS experience.
        """
    )
    assert profile.name == "Jane Candidate"
    assert profile.email == "jane@example.com"
    assert "python" in profile.skills
    assert "react" in profile.skills
    assert "developer" in profile.likely_titles


def test_rank_jobs_scores_skill_matches():
    profile = parse_cv("Jane Candidate\nPython developer with React and SQL experience.")
    job = Job(
        source="test",
        source_id="1",
        title="Python Developer",
        company="Example",
        location="Remote",
        country_hint="germany",
        url="https://example.com/job",
        description="Work with Python, React, SQL, APIs, and Docker.",
    )
    matches = rank_jobs(profile, [job], minimum_score=0)
    assert matches[0].score > 40
    assert "python" in matches[0].matched_skills


def test_one_year_candidate_cannot_match_senior_role():
    profile = replace(parse_cv("Jane\nPython AI engineer"), experience_years=1)
    job = Job("test", "1", "Senior AI Engineer", "Example", "Remote", "", "https://e/1", "Python LLM")

    matches, rejected = filter_experience_compatible(profile, rank_jobs(profile, [job], 0))

    assert matches == []
    assert rejected == 1


def test_explicit_minimum_experience_is_enforced_but_junior_is_kept():
    senior_requirement = Job(
        "test", "1", "AI Engineer", "Example", "Remote", "", "https://e/1",
        "Requires at least 3 years of professional experience with Python.",
    )
    junior = Job("test", "2", "Junior AI Engineer", "Example", "Remote", "", "https://e/2", "Python")

    assert required_experience_years(senior_requirement) == 3
    assert required_experience_years(junior) == 0


def test_five_year_candidate_rejects_internship_when_target_is_manager():
    profile = replace(
        parse_cv("Jane\nProject Management Intern with one year of experience"),
        target_position="Project Manager",
        experience_years=5,
    )
    internship = Job(
        "test", "1", "Project Manager Intern", "Example", "Colombo", "sri lanka",
        "https://e/1", "Support project planning and delivery.",
    )
    manager = Job(
        "test", "2", "Project Manager", "Example", "Colombo", "sri lanka",
        "https://e/2", "Requires 5 years of project management experience.",
    )

    kept, rejected = filter_experience_compatible(
        profile,
        rank_jobs(profile, [internship, manager], minimum_score=float("-inf")),
    )

    assert [match.job.source_id for match in kept] == ["2"]
    assert rejected == 1


def test_experienced_candidate_can_request_an_internship_explicitly():
    profile = replace(
        parse_cv("Jane\nCareer changer"),
        target_position="Project Management Internship",
        experience_years=5,
    )
    internship = Job(
        "test", "1", "Project Management Intern", "Example", "Colombo", "sri lanka",
        "https://e/1", "Support project planning and delivery.",
    )

    kept, rejected = filter_experience_compatible(
        profile,
        rank_jobs(profile, [internship], minimum_score=float("-inf")),
    )

    assert [match.job.source_id for match in kept] == ["1"]
    assert rejected == 0


def test_country_filter_rejects_global_and_foreign_jobs():
    jobs = [
        Job("local", "1", "AI Engineer", "A", "Colombo", "sri lanka", "https://e/1", ""),
        Job("remote", "2", "AI Engineer", "B", "Worldwide", "sri lanka", "https://e/2", ""),
        Job("foreign", "3", "AI Engineer", "C", "Berlin, Germany", "sri lanka", "https://e/3", ""),
        Job("local", "4", "AI Engineer", "D", "Remote", "sri lanka", "https://e/4", ""),
    ]

    kept, rejected = filter_country_compatible(jobs, "Sri Lanka")

    assert [job.source_id for job in kept] == ["1", "4"]
    assert rejected == 2


def test_country_filter_can_allow_global_remote_jobs():
    job = Job("remote", "1", "AI Engineer", "A", "Worldwide", "", "https://e/1", "")

    kept, rejected = filter_country_compatible([job], "Sri Lanka", allow_global_remote=True)

    assert kept == [job]
    assert rejected == 0


def test_global_remote_opt_in_still_rejects_foreign_onsite_and_restricted_remote():
    jobs = [
        Job("remote", "1", "AI Engineer", "A", "Worldwide", "", "https://e/1", ""),
        Job("foreign", "2", "AI Engineer", "B", "Berlin, Germany", "", "https://e/2", ""),
        Job("restricted", "3", "AI Engineer", "C", "Remote, India", "india", "https://e/3", ""),
        Job("local", "4", "AI Engineer", "D", "Colombo", "sri lanka", "https://e/4", ""),
        Job("foreign", "5", "AI Engineer", "E", "Malaysia", "sri lanka", "https://e/5", ""),
    ]

    kept, rejected = filter_country_compatible(
        jobs,
        "Sri Lanka",
        allow_global_remote=True,
    )

    assert [job.source_id for job in kept] == ["1", "4"]
    assert rejected == 3


def test_freshness_filter_rejects_stale_and_unverifiable_discovery_jobs():
    now = datetime(2026, 7, 24, tzinfo=timezone.utc)
    jobs = [
        Job("local", "1", "AI Engineer", "A", "Colombo", "sri lanka", "https://e/1", "", "2026-07-23"),
        Job("local", "2", "AI Engineer", "B", "Colombo", "sri lanka", "https://e/2", "", "2026-06-01"),
        Job("DuckDuckGo Discovery", "3", "AI Engineer", "C", "Sri Lanka", "sri lanka", "https://e/3", ""),
        Job("topjobs.lk", "4", "AI Engineer", "D", "Sri Lanka", "sri lanka", "https://e/4", ""),
    ]

    kept, rejected = filter_fresh_jobs(jobs, max_age_days=7, now=now)

    assert [job.source_id for job in kept] == ["1", "4"]
    assert rejected == 2


def test_freshness_filter_keeps_old_jobs_still_present_in_current_open_inventory():
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    jobs = [
        Job(
            "ITPro.lk",
            "1",
            "Accountant",
            "A",
            "Colombo",
            "sri lanka",
            "https://e/1",
            "",
            "2026-05-01",
        ),
        Job(
            "topjobs.lk",
            "2",
            "Registered Nurse",
            "B",
            "Sri Lanka",
            "sri lanka",
            "https://e/2",
            "",
            "2026-05-01",
        ),
        Job(
            "XpressJobs",
            "3",
            "Software Engineer",
            "C",
            "Sri Lanka",
            "sri lanka",
            "https://e/3",
            "",
            "2026-05-01",
        ),
    ]

    kept, rejected = filter_fresh_jobs(jobs, max_age_days=30, now=now)

    assert kept == jobs
    assert rejected == 0


def test_freshness_filter_still_rejects_expired_current_inventory_rows():
    job = Job(
        "topjobs.lk",
        "1",
        "Accountant",
        "A",
        "Sri Lanka",
        "sri lanka",
        "https://e/1",
        "This job expired",
        "2026-07-27",
    )

    kept, rejected = filter_fresh_jobs(
        [job],
        max_age_days=30,
        now=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )

    assert kept == []
    assert rejected == 1


@pytest.mark.parametrize(
    ("source", "description"),
    [
        ("ITPro.lk", "Still listed on today's live category inventory."),
        (
            "topjobs.lk",
            "Still in the OPEN inventory. Closing date: Tue Aug 11 2026",
        ),
        ("Arbeitnow", "Still returned by today's direct jobs API."),
        (
            "CareerLK",
            "Fetched today from a current listing and validated detail page.",
        ),
        (
            "IFS Sri Lanka Careers",
            "Still returned by the employer's complete official inventory API.",
        ),
    ],
)
def test_live_inventory_job_is_not_expired_by_old_publication_date_alone(
    source, description
):
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    job = Job(
        source,
        "still-open",
        "DevOps Engineer",
        "Example",
        "Colombo",
        "sri lanka",
        "https://example.lk/jobs/still-open",
        description,
        "2026-01-15",
    )

    kept, rejected = filter_fresh_jobs([job], max_age_days=30, now=now)

    assert kept == [job]
    assert rejected == 0


def test_freshness_filter_still_rejects_explicitly_closed_live_inventory_record():
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    job = Job(
        "ITPro.lk",
        "closed",
        "DevOps Engineer",
        "Example",
        "Colombo",
        "sri lanka",
        "https://example.lk/jobs/closed",
        "This position filled on 27 July.",
        "2026-07-27",
    )

    kept, rejected = filter_fresh_jobs([job], max_age_days=30, now=now)

    assert kept == []
    assert rejected == 1
