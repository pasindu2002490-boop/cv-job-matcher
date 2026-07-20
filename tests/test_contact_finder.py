from cv_job_matcher.contact_finder import (
    _extract_allowed_emails,
    _leads_from_search_result,
    _parse_linkedin_title,
)


def test_extract_allowed_emails_keeps_generic_recruiting_by_default():
    emails = _extract_allowed_emails("Send CVs to careers@example.lk or jane@example.lk", False)
    assert ("careers@example.lk", "generic_recruiting") in emails
    assert all(email != "jane@example.lk" for email, _ in emails)


def test_extract_allowed_emails_can_include_public_personal_emails():
    emails = _extract_allowed_emails("HR contact jane@example.lk", True)
    assert ("jane@example.lk", "public_personal") in emails


def test_parse_linkedin_title_splits_name_and_role():
    name, role = _parse_linkedin_title("Jane Perera - Talent Acquisition Partner | LinkedIn")
    assert name == "Jane Perera"
    assert role == "Talent Acquisition Partner"


def test_linkedin_search_result_keeps_public_profile_fields():
    leads = _leads_from_search_result(
        "Example Co",
        "sri lanka",
        {
            "title": "Jane Perera - Talent Acquisition Partner | LinkedIn",
            "snippet": "Talent Acquisition Partner at Example Co",
            "link": "https://www.linkedin.com/in/jane-perera",
            "thumbnail": "https://example.com/photo.jpg",
            "query": 'site:linkedin.com/in "Example Co" recruiter',
        },
        False,
    )
    assert leads[0].contact_name == "Jane Perera"
    assert leads[0].profile_url == "https://www.linkedin.com/in/jane-perera"
    assert leads[0].profile_image_url == "https://example.com/photo.jpg"
    assert "linkedin.com/search/results/companies" in leads[0].company_linkedin_search_url
