from cv_job_matcher.job_sources import _html_links, _listing_like_url, _portal_title_matches
from cv_job_matcher.models import CandidateProfile


def test_html_links_resolve_relative_job_urls():
    html = '<a class="job" href="/jobs/devops-engineer">DevOps Engineer</a>'

    assert _html_links(html, "https://example.lk/jobs") == [
        ("DevOps Engineer", "https://example.lk/jobs/devops-engineer")
    ]


def test_portal_title_matching_uses_requested_field():
    profile = CandidateProfile("CV", target_position="DevOps Engineer")

    assert _portal_title_matches(profile, "Junior DevOps Engineer")
    assert not _portal_title_matches(profile, "Accounts Executive")


def test_search_page_is_not_treated_as_an_individual_job():
    assert _listing_like_url("https://www.hire.lk/jobs?q=DevOps+Engineer")
    assert not _listing_like_url("https://www.hire.lk/jobs/123/devops-engineer")
