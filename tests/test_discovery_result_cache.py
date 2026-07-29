from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from cv_job_matcher.agent_graph import (
    AgentGraphOptions,
    VerticalJobAgentGraph,
    _clear_discovery_result_cache_for_tests,
)
from cv_job_matcher.job_sources import JobProvider
from cv_job_matcher.models import CandidateProfile, Job


def _job(role: str) -> Job:
    slug = role.casefold().replace(" ", "-")
    return Job(
        source="Counting",
        source_id=slug,
        title=role,
        company="Example",
        location="Colombo, Sri Lanka",
        country_hint="sri lanka",
        url=f"https://example.lk/jobs/{slug}",
        description=f"Current {role} vacancy in Sri Lanka",
    )


@pytest.fixture(autouse=True)
def isolated_discovery_cache(monkeypatch):
    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MINUTES", "10")
    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MAX_ENTRIES", "32")
    monkeypatch.setenv("SOURCE_AGENT_WORKERS", "1")
    _clear_discovery_result_cache_for_tests()
    yield
    _clear_discovery_result_cache_for_tests()


class RoleCountingProvider(JobProvider):
    name = "Counting"
    calls = 0
    seen_profiles: list[CandidateProfile] = []

    def search(self, profile, country, limit):
        type(self).calls += 1
        type(self).seen_profiles.append(profile)
        return [_job(profile.target_position)]


def test_candidate_specific_discovery_inputs_never_share_across_users():
    RoleCountingProvider.calls = 0
    RoleCountingProvider.seen_profiles = []
    alice = CandidateProfile(
        raw_text="Alice confidential CV",
        name="Alice",
        email="alice@example.com",
        skills=("aws", "terraform"),
        likely_titles=("platform engineer",),
        target_position="DevOps Engineer",
        experience_years=7,
    )
    bob = CandidateProfile(
        raw_text="Bob confidential CV",
        name="Bob",
        email="bob@example.com",
        skills=("azure", "kubernetes"),
        likely_titles=("site reliability engineer",),
        target_position="DevOps Engineer",
        experience_years=2,
    )

    first = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[RoleCountingProvider()],
    ).run(alice, "sri lanka")
    second = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[RoleCountingProvider()],
    ).run(bob, "sri lanka")

    assert RoleCountingProvider.calls == 2
    assert RoleCountingProvider.seen_profiles == [alice, bob]
    assert first.profile is alice
    assert second.profile is bob
    assert second.profile.raw_text == "Bob confidential CV"
    assert [trace.status for trace in second.traces] == ["completed_with_results"]
    assert any("Discovery result cache: refreshed" in note for note in second.notes)

    # An exact repeat is shareable, and caller mutation cannot alter the
    # immutable snapshot retained for that discovery contract.
    second.jobs.clear()
    second.notes.append("caller-only mutation")
    third = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[RoleCountingProvider()],
    ).run(bob, "sri lanka")
    assert len(third.jobs) == 1
    assert RoleCountingProvider.calls == 2
    assert any("Discovery result cache: hit" in note for note in third.notes)
    assert "caller-only mutation" not in third.notes
    assert third.jobs is not first.jobs
    assert third.traces is not first.traces


class KeyCountingProvider(JobProvider):
    name = "Key counting"
    calls = 0

    def search(self, profile, country, limit):
        type(self).calls += 1
        return [_job(profile.target_position)]


def test_discovery_cache_key_separates_all_discovery_inputs(monkeypatch):
    KeyCountingProvider.calls = 0
    profile = CandidateProfile(raw_text="one", target_position="Accountant")

    def run(
        *,
        country: str = "sri lanka",
        limit: int = 10,
        remote: bool = False,
        web: bool = False,
        role: str = "Accountant",
        raw_text: str = "candidate-specific",
    ):
        return VerticalJobAgentGraph(
            AgentGraphOptions(
                limit_per_source=limit,
                include_remote_global=remote,
                web_discovery=web,
            ),
            providers=[KeyCountingProvider()],
        ).run(
            CandidateProfile(raw_text=raw_text, target_position=role),
            country,
        )

    run()
    run()  # identical discovery contract
    assert KeyCountingProvider.calls == 1

    run(country="india")
    run(limit=20)
    run(remote=True)
    run(web=True)
    run(role="Registered Nurse")
    run(raw_text="different source-discovery input")
    assert KeyCountingProvider.calls == 7

    # Provider behavior can depend on arbitrary environment configuration.
    monkeypatch.setenv("TEST_DISCOVERY_ENDPOINT", "https://other.example")
    run()
    assert KeyCountingProvider.calls == 8


class ExpiringProvider(JobProvider):
    name = "Expiring"
    calls = 0

    def search(self, profile, country, limit):
        type(self).calls += 1
        return [_job(profile.target_position)]


def test_discovery_cache_expires_and_evicts_lru(monkeypatch):
    import cv_job_matcher.agent_graph as agent_graph

    ExpiringProvider.calls = 0
    clock = [0.0]
    monkeypatch.setattr(agent_graph.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MINUTES", "1")
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[ExpiringProvider()],
    )
    devops = CandidateProfile(raw_text="CV", target_position="DevOps Engineer")

    graph.run(devops, "sri lanka")
    clock[0] = 59.0
    graph.run(devops, "sri lanka")
    assert ExpiringProvider.calls == 1

    clock[0] = 61.0
    graph.run(devops, "sri lanka")
    assert ExpiringProvider.calls == 2

    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MAX_ENTRIES", "1")
    clock[0] = 62.0
    graph.run(
        CandidateProfile(raw_text="CV", target_position="Accountant"),
        "sri lanka",
    )
    clock[0] = 63.0
    graph.run(devops, "sri lanka")
    assert ExpiringProvider.calls == 4


class SlowProvider(JobProvider):
    name = "Slow"
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def search(self, profile, country, limit):
        type(self).calls += 1
        type(self).started.set()
        if not type(self).release.wait(timeout=3):
            raise TimeoutError("test did not release provider")
        return [_job(profile.target_position)]


def test_concurrent_identical_requests_use_single_flight():
    SlowProvider.calls = 0
    SlowProvider.started = threading.Event()
    SlowProvider.release = threading.Event()
    profile = CandidateProfile(raw_text="CV", target_position="Software Engineer")
    first_graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[SlowProvider()],
    )
    second_graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[SlowProvider()],
    )
    second_began = threading.Event()

    def run_second():
        second_began.set()
        return second_graph.run(profile, "sri lanka")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_graph.run, profile, "sri lanka")
        assert SlowProvider.started.wait(timeout=1)
        second_future = executor.submit(run_second)
        assert second_began.wait(timeout=1)
        # Give the second graph time to join the in-progress cache flight.
        time.sleep(0.05)
        SlowProvider.release.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert SlowProvider.calls == 1
    assert first.jobs == second.jobs
    assert any(
        "Discovery result cache: single-flight reuse" in note
        for note in second.notes
    )


class AuditedFailureProvider(JobProvider):
    name = "Audited failure"
    calls = 0

    def search(self, profile, country, limit):
        type(self).calls += 1
        raise TimeoutError("portal timed out")


def test_cache_hit_preserves_failed_source_audit_and_capture_time():
    AuditedFailureProvider.calls = 0
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[AuditedFailureProvider()],
    )
    profile = CandidateProfile(raw_text="CV", target_position="Accountant")

    graph.run(profile, "sri lanka")
    cached = graph.run(profile, "sri lanka")

    assert AuditedFailureProvider.calls == 1
    assert cached.traces[0].status == "failed"
    assert cached.traces[0].note == "portal timed out"
    cache_note = next(
        note for note in cached.notes if note.startswith("Discovery result cache:")
    )
    assert "hit" in cache_note
    assert "captured" in cache_note


def test_cache_can_be_disabled(monkeypatch):
    KeyCountingProvider.calls = 0
    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MINUTES", "0")
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[KeyCountingProvider()],
    )
    profile = CandidateProfile(raw_text="CV", target_position="Accountant")

    graph.run(profile, "sri lanka")
    graph.run(profile, "sri lanka")

    assert KeyCountingProvider.calls == 2


def test_invalid_cache_environment_uses_safe_defaults(monkeypatch):
    KeyCountingProvider.calls = 0
    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MINUTES", "not-a-number")
    monkeypatch.setenv("DISCOVERY_RESULT_CACHE_MAX_ENTRIES", "also-invalid")
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(),
        providers=[KeyCountingProvider()],
    )
    profile = CandidateProfile(raw_text="CV", target_position="Accountant")

    first = graph.run(profile, "sri lanka")
    second = graph.run(profile, "sri lanka")

    assert first.jobs == second.jobs
    assert KeyCountingProvider.calls == 1
    assert any("Discovery result cache: hit" in note for note in second.notes)
