from cv_job_matcher.it_company_sources import (
    SRI_LANKA_IT_COMPANY_CAREER_SEEDS,
    VERIFIED_IT_COMPANY_CAREER_URLS,
    UnverifiedITCareerProvider,
    is_it_position,
    it_company_career_providers,
)


def test_it_company_registry_contains_exactly_100_unique_sources():
    assert len(SRI_LANKA_IT_COMPANY_CAREER_SEEDS) == 100
    assert len({name.casefold() for name, _ in SRI_LANKA_IT_COMPANY_CAREER_SEEDS}) == 100
    assert len({url.casefold() for _, url in SRI_LANKA_IT_COMPANY_CAREER_SEEDS}) == 100
    assert len(it_company_career_providers()) == 100
    assert len(VERIFIED_IT_COMPANY_CAREER_URLS) == 65
    assert sum(
        isinstance(provider, UnverifiedITCareerProvider)
        for provider in it_company_career_providers()
    ) == 35


def test_it_position_classifier_routes_technical_roles_only():
    for position in (
        "AI/ML Engineer",
        "Senior Software Engineer",
        "Data Scientist",
        "DevOps Engineer",
        "Cybersecurity Analyst",
        "IT Support Officer",
    ):
        assert is_it_position(position), position

    for position in (
        "Accountant",
        "Finance Manager",
        "HR Intern",
        "Civil Engineer",
        "Marketing Executive",
    ):
        assert not is_it_position(position), position
