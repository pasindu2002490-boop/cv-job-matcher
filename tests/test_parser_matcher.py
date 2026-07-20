from cv_job_matcher.cv_parser import parse_cv
from cv_job_matcher.matcher import rank_jobs
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

