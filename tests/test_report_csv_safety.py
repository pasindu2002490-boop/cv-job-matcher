import csv
from pathlib import Path

from cv_job_matcher.agent_graph import SourceAgentTrace
from cv_job_matcher.models import CandidateProfile, ContactLead, Job, MatchResult
from cv_job_matcher.report import (
    _write_companies_csv,
    _write_contacts_csv,
    write_outputs,
)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _assert_formula_safe(rows: list[dict[str, str]]) -> None:
    for row in rows:
        for value in row.values():
            assert not value.lstrip().startswith(("=", "+", "-", "@")), value


def _malicious_contact() -> ContactLead:
    return ContactLead(
        company="+Example",
        contact_name="@SUM(A1:A2)",
        title="  =1+1",
        email="-cmd@example.test",
        email_type="+public",
        confidence="@verified",
        company_linkedin_search_url="=HYPERLINK(\"https://example.test\")",
        linkedin_search_url="  +https://example.test/hr",
        profile_url="-https://example.test/profile",
        profile_image_url="@https://example.test/photo",
        source_url="=https://example.test/source",
        search_query="+recruiter",
        evidence=" \t-unsafe evidence",
    )


def test_all_public_report_csvs_escape_untrusted_formula_cells(
    tmp_path: Path,
) -> None:
    normal_url = "https://example.lk/jobs/123"
    job = Job(
        source="=Source",
        source_id="1",
        title="  =HYPERLINK(\"https://evil.test\")",
        company="+Example",
        location="-1+1",
        country_hint="@Sri Lanka",
        url=normal_url,
        description="Role description",
        published_at=" =TODAY()",
        salary="@SUM(A1:A2)",
        job_type="+Full time",
    )
    match = MatchResult(
        job=job,
        score=80,
        matched_skills=("=python",),
        matched_title_terms=("engineer",),
        concerns=(" \t+untrusted concern",),
        llm_decision="-keep",
        llm_reason=" @untrusted reason",
        llm_provider="+provider",
        llm_model="=model",
    )
    trace = SourceAgentTrace(
        source=job.source,
        connector=" +connector",
        status="-untrusted-status",  # type: ignore[arg-type]
        discovered=1,
        note="@untrusted note",
    )

    write_outputs(
        tmp_path,
        CandidateProfile(raw_text="CV", target_position="Engineer"),
        "sri lanka",
        [job],
        [match],
        [],
        "Tailored CV",
        contact_leads=[_malicious_contact()],
        related_matches=[match],
        rejected_matches=[match],
        manual_review_matches=[match],
        source_traces=[trace],
    )

    csv_names = (
        "all_discovered_jobs.csv",
        "related_vacancies.csv",
        "job_matches.csv",
        "rejected_vacancies.csv",
        "manual_review_vacancies.csv",
        "source_coverage.csv",
    )
    for name in csv_names:
        _assert_formula_safe(_read_rows(tmp_path / name))

    match_row = _read_rows(tmp_path / "job_matches.csv")[0]
    related_row = _read_rows(tmp_path / "related_vacancies.csv")[0]
    assert match_row["title"] == "'  =HYPERLINK(\"https://evil.test\")"
    assert match_row["company"] == "'+Example"
    assert match_row["concerns"] == "' \t+untrusted concern"
    assert match_row["apply_url"] == normal_url
    assert "llm_reason" not in match_row
    assert "hr_contact_name" not in match_row
    assert list(related_row) == [
        "match_score",
        "title",
        "company",
        "location",
        "source",
        "published_at",
        "fetched_at_utc",
        "detail_page_verified",
        "matched_skills",
        "concerns",
        "apply_url",
    ]
    assert related_row["apply_url"] == normal_url


def test_company_and_contact_csv_writers_use_same_safe_boundary(
    tmp_path: Path,
) -> None:
    contact = _malicious_contact()
    job = Job(
        source="@Source",
        source_id="1",
        title="-Engineer",
        company=contact.company,
        location=" =Colombo",
        country_hint="sri lanka",
        url="https://example.lk/jobs/1",
        description="Role description",
        published_at="+TODAY()",
    )
    match = MatchResult(job, 70, (), ())
    contact_by_company = {contact.company.lower(): contact}
    companies_path = tmp_path / "companies.csv"
    contacts_path = tmp_path / "contacts.csv"

    _write_companies_csv(companies_path, [match], contact_by_company)
    _write_contacts_csv(contacts_path, [contact])

    company_rows = _read_rows(companies_path)
    contact_rows = _read_rows(contacts_path)
    _assert_formula_safe(company_rows)
    _assert_formula_safe(contact_rows)
    assert company_rows[0]["apply_url"] == "https://example.lk/jobs/1"
    assert contact_rows[0]["company"] == "'+Example"
    assert contact_rows[0]["evidence"] == "' \t-unsafe evidence"
