from cv_job_matcher.cv_parser import parse_cv
from dataclasses import replace

from cv_job_matcher.matcher import filter_experience_compatible, rank_jobs, required_experience_years
from cv_job_matcher.models import Job


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
