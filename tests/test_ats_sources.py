import json

from cv_job_matcher import ats_sources
from cv_job_matcher.ats_sources import (
    BistecZohoCareerProvider,
    IfsCareerProvider,
    InforCareerProvider,
    ManatalCareerProvider,
    RootcodeApiProvider,
    SmartRecruitersProvider,
    ThreeCsCareerProvider,
)
from cv_job_matcher.models import CandidateProfile


def test_manatal_provider_uses_structured_jobs_and_role_gate(monkeypatch):
    payload = {"results": [
        {"id": 1, "hash": "AI1", "position_name": "AI Engineer", "country": "Sri Lanka", "location_display": "Colombo, Sri Lanka", "description": "<p>Python ML role</p>"},
        {"id": 2, "hash": "FIN1", "position_name": "Finance Manager", "country": "Sri Lanka", "location_display": "Colombo", "description": "Finance"},
    ]}
    monkeypatch.setattr(ats_sources, "_get", lambda _url: json.dumps(payload))
    jobs = ManatalCareerProvider("MIT Careers", "mitesp", "MIT").search(
        CandidateProfile(raw_text="", target_position="AI Engineer"), "sri lanka", 10
    )
    assert [job.title for job in jobs] == ["AI Engineer"]
    assert jobs[0].url.endswith("/mitesp/job/AI1")


def test_smartrecruiters_provider_keeps_sri_lanka_rows(monkeypatch):
    listing = {"content": [
        {"id": "1", "name": "AI Engineer", "location": {"country": "lk", "fullLocation": "Colombo, Sri Lanka"}, "ref": "detail", "releasedDate": "2026-08-08", "typeOfEmployment": {"label": "Full-time"}},
        {"id": "2", "name": "AI Engineer", "location": {"country": "us", "fullLocation": "Boston, United States"}, "ref": "other"},
    ]}
    detail = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Machine learning</p>"}}}}
    monkeypatch.setattr(ats_sources, "_get", lambda url: json.dumps(detail if url == "detail" else listing))
    jobs = SmartRecruitersProvider("Acumatica Careers", "acumatica", "Acumatica").search(
        CandidateProfile(raw_text="", target_position="AI Engineer"), "sri lanka", 10
    )
    assert len(jobs) == 1
    assert jobs[0].location == "Colombo, Sri Lanka"
    assert jobs[0].detail_page_verified


def test_rootcode_provider_reads_complete_api_and_builds_canonical_url(monkeypatch):
    payload = {"results": [
        {"id": 1206830, "position_name": "Intern - Software Engineer Fullstack", "country": "Sri Lanka", "location_display": "Colombo, WP, Sri Lanka", "description": "<p>Build software</p>"},
        {"id": 2, "position_name": "Growth Executive", "country": "Sri Lanka", "location_display": "Colombo", "description": "Marketing"},
        {"id": 3, "position_name": "Software Engineer", "country": "Estonia", "location_display": "Tallinn", "description": "Software"},
    ]}
    monkeypatch.setattr(ats_sources, "_get", lambda _url: json.dumps(payload))
    provider = RootcodeApiProvider()
    jobs = provider.search(
        CandidateProfile(raw_text="", target_position="Software Engineer"),
        "sri lanka",
        10,
    )
    assert provider.last_inventory_count == 2
    assert [job.title for job in jobs] == ["Intern - Software Engineer Fullstack"]
    assert jobs[0].url == "https://rootcode.ai/careers/intern-software-engineer-fullstack-1206830"


def test_ifs_provider_filters_country_and_fetches_details(monkeypatch):
    listing = {"jobs": [
        {"id": "1", "name": "Software Engineer", "location": {"country": "lk", "fullLocation": "Colombo, Sri Lanka"}, "ref": "detail", "typeOfEmployment": {"label": "Full-time"}},
        {"id": "2", "name": "Software Engineer", "location": {"country": "us"}, "ref": "other"},
    ]}
    detail = {"jobAd": {"sections": {"description": {"text": "<p>Build cloud software</p>"}}}}
    monkeypatch.setattr(ats_sources, "_get", lambda url: json.dumps(detail if url == "detail" else listing))
    provider = IfsCareerProvider()
    jobs = provider.search(CandidateProfile(raw_text="", target_position="Software Engineer"), "sri lanka", 10)
    assert provider.last_inventory_count == 1
    assert [job.description for job in jobs] == ["Build cloud software"]


def test_infor_provider_uses_embedded_full_descriptions(monkeypatch):
    payload = {"data": [
        {"id": 1, "title": "Software Engineer", "location": "Colombo, Sri Lanka", "description": "<p>Python</p>", "path": "/jobs/1"},
        {"id": 2, "title": "Software Engineer", "location": "London", "description": "Java"},
    ]}
    monkeypatch.setattr(ats_sources, "_get", lambda _url: json.dumps(payload))
    provider = InforCareerProvider()
    jobs = provider.search(CandidateProfile(raw_text="", target_position="Software Engineer"), "sri lanka", 10)
    assert provider.last_inventory_count == 1
    assert jobs[0].url == "https://careers.infor.com/jobs/1"


def test_bistec_provider_reads_zoho_public_inventory(monkeypatch):
    payload = {"data": [{"id": "z1", "Posting_Title": "Software Engineer", "City": "Colombo", "Country": "Sri Lanka", "Job_Description": "<p>React</p>", "$url": "https://example/jobs/z1"}]}
    monkeypatch.setattr(ats_sources, "_get", lambda _url: json.dumps(payload))
    jobs = BistecZohoCareerProvider().search(
        CandidateProfile(raw_text="", target_position="Software Engineer"), "sri lanka", 10
    )
    assert jobs[0].description == "React"
    assert jobs[0].url == "https://example/jobs/z1"


def test_three_cs_provider_reads_static_json(monkeypatch):
    payload = [{"title": "Software Engineer", "description": "Web apps", "requirements": "Python", "applyLink": "/apply/1"}]
    monkeypatch.setattr(ats_sources, "_get", lambda _url: json.dumps(payload))
    jobs = ThreeCsCareerProvider().search(
        CandidateProfile(raw_text="", target_position="Software Engineer"), "sri lanka", 10
    )
    assert jobs[0].url == "https://www.3cs.lk/apply/1"
    assert "Python" in jobs[0].description
