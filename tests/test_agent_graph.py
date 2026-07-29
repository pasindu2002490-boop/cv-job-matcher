from cv_job_matcher.agent_graph import (
    AgentGraphOptions,
    VerticalJobAgentGraph,
    _dedupe_jobs,
)
from cv_job_matcher.job_sources import AdzunaProvider, JobProvider
from cv_job_matcher.models import CandidateProfile, Job


class WorkingProvider(JobProvider):
    name = "Working"

    def search(self, profile, country, limit):
        return [
            Job(
                source=self.name,
                source_id="1",
                title="AI Engineer",
                company="Example",
                location="Colombo, Sri Lanka",
                country_hint="sri lanka",
                url="https://example.lk/jobs/1",
                description="Python machine learning role",
                published_at="2026-07-28",
            )
        ]


class BrokenProvider(JobProvider):
    name = "Broken"

    def search(self, profile, country, limit):
        raise TimeoutError("portal timeout")


class EmptyProvider(JobProvider):
    name = "Empty"

    def search(self, profile, country, limit):
        return []


class CompleteInventoryNoRoleProvider(JobProvider):
    name = "Complete inventory"
    last_inventory_count = 417

    def search(self, profile, country, limit):
        return []


def test_vertical_graph_isolates_source_failure_and_continues():
    profile = CandidateProfile(
        raw_text="AI Engineer Python machine learning",
        skills=("python", "machine learning"),
        likely_titles=("ai engineer",),
        target_position="AI Engineer",
        experience_years=2,
    )
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(minimum_score=0),
        providers=[BrokenProvider(), WorkingProvider()],
    )

    state = graph.run(profile, "sri lanka")

    assert [trace.status for trace in state.traces] == ["failed", "completed_with_results"]
    assert [job.url for job in state.jobs] == ["https://example.lk/jobs/1"]
    assert "Discovery fan-in" in state.notes[-1]


def test_vertical_graph_marks_zero_rows_as_unverified_not_empty_website():
    profile = CandidateProfile(raw_text="CV", target_position="AI Engineer")
    state = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[EmptyProvider()],
    ).run(profile, "sri lanka")

    assert state.traces[0].status == "connector_empty_unverified"
    assert state.traces[0].discovered == 0
    assert "website inventory not verified empty" in state.notes[0]


def test_vertical_graph_distinguishes_complete_inventory_with_no_role_candidates():
    profile = CandidateProfile(raw_text="CV", target_position="Registered Nurse")
    state = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[CompleteInventoryNoRoleProvider()],
    ).run(profile, "sri lanka")

    trace = state.traces[0]
    assert trace.status == "completed_inventory_no_role_candidates"
    assert trace.inventory_total == 417
    assert "complete current inventory loaded (417 rows)" in trace.note


def test_vertical_graph_flags_a_source_result_cap():
    profile = CandidateProfile(raw_text="CV", target_position="Engineer")
    state = VerticalJobAgentGraph(
        AgentGraphOptions(limit_per_source=1),
        providers=[WorkingProvider()],
    ).run(profile, "sri lanka")

    assert "configured result cap reached" in state.traces[0].note


def test_vertical_graph_marks_missing_credentials_as_skipped(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    profile = CandidateProfile(raw_text="CV", target_position="AI Engineer")

    state = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[AdzunaProvider()],
    ).run(profile, "sri lanka")

    assert state.traces[0].status == "skipped"
    assert state.traces[0].note == "credentials not configured"


def test_graph_manifest_preserves_vertical_source_order():
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[WorkingProvider(), BrokenProvider()],
    )

    manifest = graph.manifest()

    assert '"direction": "fan-out/fan-in"' in manifest
    assert manifest.index('"Working"') < manifest.index('"Broken"')


def test_dedupe_removes_tracking_variants_and_prefers_richer_syndicated_copy():
    shared_description = (
        "Build and operate cloud infrastructure with AWS, Kubernetes, Terraform, "
        "CI/CD pipelines, observability, incident response, Linux automation, and "
        "security controls while partnering with software engineering teams."
    )
    jobs = [
        Job(
            "One",
            "1",
            "DevOps Engineer",
            "Example (Pvt) Ltd.",
            "Colombo",
            "sri lanka",
            "https://example.lk/jobs/1?utm_source=mail",
            shared_description,
        ),
        Job(
            "One",
            "1b",
            "DevOps Engineer",
            "Example (Pvt) Ltd.",
            "Colombo",
            "sri lanka",
            "https://example.lk/jobs/1?utm_source=social&utm_campaign=test",
            shared_description + " This source also lists an on-call rotation.",
        ),
        Job(
            "Syndicator",
            "99",
            "  DevOps   Engineer ",
            "EXAMPLE PVT LTD",
            "Colombo, Sri Lanka",
            "sri lanka",
            "https://jobs.example.com/opening/99",
            shared_description
            + " This source also lists an on-call rotation and application details.",
        ),
    ]

    deduped = _dedupe_jobs(jobs)

    assert len(deduped) == 1
    assert deduped[0].source == "Syndicator"


def test_dedupe_preserves_distinct_requisitions_with_same_title_and_company():
    jobs = [
        Job(
            "Direct",
            "colombo",
            "Software Engineer",
            "Example Ltd",
            "Colombo",
            "sri lanka",
            "https://example.lk/jobs/colombo",
            "Build the payments platform using Java and PostgreSQL for the Colombo team.",
        ),
        Job(
            "Direct",
            "kandy",
            "Software Engineer",
            "Example Ltd",
            "Kandy",
            "sri lanka",
            "https://example.lk/jobs/kandy",
            "Build embedded systems using C++ and Linux for the Kandy engineering team.",
        ),
    ]

    deduped = _dedupe_jobs(jobs)

    assert [job.source_id for job in deduped] == ["colombo", "kandy"]


def test_dedupe_does_not_merge_anonymous_cross_source_listings():
    description = (
        "Maintain infrastructure, deployment pipelines, Kubernetes clusters, "
        "monitoring systems, and production incident response for the platform."
    )
    jobs = [
        Job(
            "Board A",
            "1",
            "DevOps Engineer",
            "Confidential",
            "Colombo",
            "sri lanka",
            "https://a.example/jobs/1",
            description,
        ),
        Job(
            "Board B",
            "2",
            "DevOps Engineer",
            "Confidential",
            "Colombo",
            "sri lanka",
            "https://b.example/jobs/2",
            description,
        ),
    ]

    assert len(_dedupe_jobs(jobs)) == 2
