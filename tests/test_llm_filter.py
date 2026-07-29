from io import BytesIO
import json
from dataclasses import replace
from urllib.error import HTTPError, URLError

import pytest

from cv_job_matcher import llm_filter
from cv_job_matcher.models import CandidateProfile, Job, MatchResult


def test_strict_llm_filter_batches_every_job_and_drops_maybe(monkeypatch):
    profile = CandidateProfile("CV", target_position="AI Engineer", experience_years=1)
    matches = [
        MatchResult(Job("test", str(i), "AI Engineer", "Co", "Remote", "", f"https://e/{i}", ""), 50, (), ())
        for i in range(5)
    ]
    reviewed = []

    monkeypatch.setattr(llm_filter, "_resolve_llm_config", lambda model, provider: ("Groq", "key", "url", model))

    def fake_call(
        profile,
        batch,
        api_key,
        endpoint,
        model,
        country="",
        allow_global_remote=False,
    ):
        reviewed.extend(item.job.url for item in batch)
        return [
            {
                "review_id": llm_filter._review_id(item),
                "url": item.job.url,
                "decision": "maybe" if item.job.source_id == "3" else "keep",
                "score": 80,
                "reason": "test",
            }
            for item in batch
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", fake_call)
    # Strict mode reviews all jobs even when the optional CLI limit is lower.
    rejected = []
    kept, note = llm_filter.apply_llm_filter(
        profile, matches, True, "model", 2, "groq", True, 2,
        rejected_audit=rejected,
    )

    assert len(reviewed) == 5
    assert len(kept) == 4
    assert all(item.job.source_id != "3" for item in kept)
    assert [item.job.source_id for item in rejected] == ["3"]
    assert rejected[0].llm_decision == "maybe"
    assert "reviewed 5 job(s)" in note


def test_403_job_is_isolated_for_manual_review_without_false_rejection(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "0")
    profile = CandidateProfile("private CV text", target_position="AI Engineer", experience_years=1)
    matches = [
        MatchResult(Job("test", str(i), "AI Engineer", "Co", "Remote", "", f"https://e/{i}", "description"), 50, (), ())
        for i in range(3)
    ]
    monkeypatch.setattr(llm_filter, "_resolve_llm_config", lambda model, provider: ("Groq", "key", "url", model))

    def fake_call(
        profile,
        batch,
        api_key,
        endpoint,
        model,
        country="",
        allow_global_remote=False,
    ):
        if any(item.job.source_id == "1" for item in batch):
            raise HTTPError("url", 403, "Forbidden", {}, BytesIO(b"Forbidden"))
        return [
            {
                "review_id": llm_filter._review_id(item),
                "url": item.job.url,
                "decision": "keep",
                "score": 80,
                "reason": "fit",
            }
            for item in batch
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", fake_call)
    rejected = []
    manual_review = []
    kept, note = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "model",
        10,
        "groq",
        True,
        3,
        rejected_audit=rejected,
        manual_review_audit=manual_review,
    )

    assert [item.job.source_id for item in kept] == ["0", "2"]
    assert rejected == []
    assert [item.job.source_id for item in manual_review] == ["1"]
    assert manual_review[0].llm_decision == "review_failed"
    assert manual_review[0].llm_reason.startswith("Manual review required:")
    assert "provider-blocked job(s)" in note
    assert "1 job(s) require manual review" in note


@pytest.mark.parametrize(
    ("provider_argument", "provider_name"),
    [("groq", "Groq"), ("openai", "OpenAI")],
)
def test_strict_remote_malformed_batch_splits_and_singleton_needs_manual_review(
    monkeypatch,
    provider_argument,
    provider_name,
):
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "0")
    profile = CandidateProfile("CV", target_position="Accountant", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Accountant",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://e/{index}",
                "Prepare accounts.",
            ),
            50,
            (),
            (),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: (provider_name, "key", "url", model),
    )
    calls = []

    def incomplete_call(profile, batch, *args, **kwargs):
        calls.append([item.job.source_id for item in batch])
        if len(batch) > 1 or batch[0].job.source_id == "1":
            reviewed = matches[0]
        else:
            reviewed = batch[0]
        return [
            {
                "review_id": llm_filter._review_id(reviewed),
                "url": reviewed.job.url,
                "decision": "keep",
                "score": 75,
                "reason": "fit",
            }
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", incomplete_call)
    rejected = []
    manual_review = []
    kept, note = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "model",
        1,
        provider_argument,
        True,
        5,
        rejected_audit=rejected,
        manual_review_audit=manual_review,
    )

    assert calls == [["0", "1"], ["0"], ["1"]]
    assert [item.job.source_id for item in kept] == ["0"]
    assert rejected == []
    assert [item.job.source_id for item in manual_review] == ["1"]
    assert manual_review[0].llm_decision == "review_failed"
    assert manual_review[0].llm_provider == provider_name
    assert "1 job(s) require manual review" in note


def test_non_strict_provider_failure_preserves_prior_rejection_partition(
    monkeypatch,
):
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "0")
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Engineer",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://e/{index}",
                "Engineering role.",
            ),
            50,
            (),
            (),
        )
        for index in range(4)
    ]
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )

    def fail_after_rejection(profile, batch, *args, **kwargs):
        if batch[0].job.source_id == "1":
            raise URLError("offline")
        return [
            {
                "url": batch[0].job.url,
                "decision": "reject",
                "score": 10,
                "reason": "not a fit",
            }
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", fail_after_rejection)
    rejected = []
    manual_review = []
    completed = []

    kept, note = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "model",
        3,
        "groq",
        False,
        1,
        rejected_audit=rejected,
        manual_review_audit=manual_review,
        completed_audit=completed,
    )

    assert [item.job.source_id for item in kept] == ["3"]
    assert [item.job.source_id for item in rejected] == ["0"]
    assert [item.job.source_id for item in manual_review] == ["1", "2"]
    assert all(item.llm_decision == "review_failed" for item in manual_review)
    partition = kept + rejected + manual_review
    assert sorted(item.job.source_id for item in partition) == ["0", "1", "2", "3"]
    assert len({llm_filter._review_id(item) for item in partition}) == len(partition)
    assert [item.job.source_id for item in completed] == ["0", "1", "2"]
    assert "2 job(s) require manual review" in note


def test_review_ids_keep_rows_distinct_even_when_urls_match(monkeypatch):
    profile = CandidateProfile("CV", target_position="Accountant", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Accountant",
                f"Company {index}",
                "Colombo, Sri Lanka",
                "sri lanka",
                "",
                "Prepare accounts.",
            ),
            50,
            (),
            (),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )

    def fake_call(profile, batch, *args, **kwargs):
        return [
            {
                "review_id": llm_filter._review_id(item),
                "url": item.job.url,
                "decision": "keep" if item.job.source_id == "0" else "reject",
                "score": 75,
                "reason": "reviewed separately",
            }
            for item in batch
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", fake_call)
    rejected = []

    kept, _ = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "model",
        1,
        "groq",
        True,
        5,
        rejected_audit=rejected,
    )

    assert [item.job.source_id for item in kept] == ["0"]
    assert [item.job.source_id for item in rejected] == ["1"]


def test_strict_mode_cannot_be_silently_disabled():
    with pytest.raises(RuntimeError, match="Strict LLM filtering"):
        llm_filter.apply_llm_filter(
            CandidateProfile("CV"),
            [],
            enabled=False,
            model="model",
            limit=1,
            strict=True,
        )


def test_llm_payload_contains_full_available_candidate_and_job_evidence(
    monkeypatch,
):
    profile = CandidateProfile(
        raw_text="Complete extracted CV",
        skills=("python",),
        likely_titles=("software engineer",),
        experience_lines=("Software Engineer — 2 years",),
        target_position="Software Engineer",
        experience_years=2,
    )
    job = Job(
        "topjobs.lk",
        "req-123",
        "Software Engineer",
        "Example",
        "Colombo, Sri Lanka",
        "sri lanka",
        "https://e/req-123",
        "Complete vacancy description with requirements.",
        published_at="2026-07-28",
        salary="LKR 200,000",
        job_type="Full time",
    )
    captured = {}

    def fake_post(endpoint, api_key, body):
        captured["body"] = body
        return [
            {
                "url": job.url,
                "decision": "keep",
                "score": 90,
                "reason": "fit",
            }
        ]

    monkeypatch.setattr(llm_filter, "_post_chat_completion", fake_post)

    llm_filter._call_llm(
        profile,
        [MatchResult(job, 80, ("python",), ("software engineer",))],
        "key",
        "https://llm.example/v1",
        "model",
        country="sri lanka",
        allow_global_remote=True,
    )

    prompt = json.loads(captured["body"]["messages"][1]["content"])
    candidate = prompt["candidate"]
    evidence = prompt["jobs"][0]
    assert candidate["cv_text"] == profile.raw_text
    assert candidate["likely_titles"] == ["software engineer"]
    assert candidate["experience_lines"] == ["Software Engineer — 2 years"]
    assert candidate["target_country"] == "sri lanka"
    assert candidate["allow_worldwide_remote"] is True
    assert {
        "source_id",
        "country_hint",
        "published_at",
        "job_type",
        "salary",
        "description",
    } <= evidence.keys()
    assert "fetched_at_utc" not in evidence
    assert evidence["description"] == job.description


def test_bounded_evidence_preserves_the_start_and_end():
    evidence = "A" * 800 + "B" * 800

    bounded = llm_filter._bounded_evidence(evidence, 700)

    assert len(bounded) == 700
    assert bounded.startswith("A")
    assert bounded.endswith("B")
    assert "middle characters omitted" in bounded


def test_groq_failure_switches_failed_and_remaining_batches_to_ollama(monkeypatch):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Engineer",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://e/{index}",
                "Engineering role.",
            ),
            50,
            (),
            (),
        )
        for index in range(5)
    ]
    groq_batches = []
    ollama_batches = []
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "https://groq.test/v1", model),
    )

    def fake_groq(profile, batch, *args, **kwargs):
        groq_batches.append([item.job.source_id for item in batch])
        if batch[0].job.source_id == "2":
            raise HTTPError(
                "url",
                429,
                "Too Many Requests",
                {},
                BytesIO(b'{"secret":"provider-body-must-not-leak"}'),
            )
        return _keep_decisions(batch)

    def fake_ollama(profile, batch, *args, **kwargs):
        ollama_batches.append([item.job.source_id for item in batch])
        return _keep_decisions(batch)

    monkeypatch.setattr(llm_filter, "_call_llm", fake_groq)
    monkeypatch.setattr(llm_filter, "_call_ollama", fake_ollama)
    monkeypatch.setattr(
        llm_filter.time,
        "sleep",
        lambda *_: pytest.fail("hybrid mode must not sleep on the first Groq 429"),
    )

    kept, note = llm_filter.apply_llm_filter(
        profile,
        matches,
        enabled=True,
        model="groq-model",
        limit=1,
        provider="groq",
        strict=True,
        batch_size=2,
        rejected_audit=[],
    )

    assert groq_batches == [["0", "1"], ["2", "3"]]
    assert ollama_batches == [["2", "3"], ["4"]]
    assert [item.job.source_id for item in kept] == ["0", "1", "2", "3", "4"]
    assert [(item.llm_provider, item.llm_model) for item in kept[:2]] == [
        ("Groq", "groq-model"),
        ("Groq", "groq-model"),
    ]
    assert all(item.llm_provider == "Ollama" for item in kept[2:])
    assert "Groq/groq-model reviewed 2" in note
    assert "Ollama/llama3.1:8b reviewed 3" in note
    assert "Groq rate or quota limit (HTTP 429)" in note
    assert "provider-body-must-not-leak" not in note


def test_ollama_retries_one_malformed_singleton_then_continues(monkeypatch):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Engineer",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://e/{index}",
                "Engineering role.",
            ),
            50,
            (),
            (),
        )
        for index in range(2)
    ]
    calls = []
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_JSON_RETRIES", "1")
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )
    monkeypatch.setattr(
        llm_filter,
        "_call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    def fake_ollama(profile, batch, *args, **kwargs):
        calls.append(batch[0].job.source_id)
        if calls == ["0"]:
            row = _keep_decisions(batch)[0]
            row["review_id"] = "wrong"
            return [row]
        return _keep_decisions(batch)

    monkeypatch.setattr(llm_filter, "_call_ollama", fake_ollama)

    kept, note = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "groq-model",
        1,
        "groq",
        True,
        2,
        rejected_audit=[],
    )

    assert calls == ["0", "0", "1"]
    assert [item.job.source_id for item in kept] == ["0", "1"]
    assert all(item.llm_provider == "Ollama" for item in kept)
    assert "Groq connection failure" in note


def test_strict_ollama_malformed_after_retry_becomes_manual_review(
    monkeypatch,
):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    match = MatchResult(
        Job(
            "test",
            "1",
            "Engineer",
            "Co",
            "Colombo, Sri Lanka",
            "sri lanka",
            "https://e/1",
            "Engineering role.",
        ),
        50,
        (),
        (),
    )
    ollama_calls = []
    rejected = []
    manual_review = []
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_JSON_RETRIES", "1")
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )
    monkeypatch.setattr(
        llm_filter,
        "_call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    def malformed_ollama(profile, batch, *args, **kwargs):
        ollama_calls.append(batch[0].job.source_id)
        row = _keep_decisions(batch)[0]
        row["review_id"] = "unknown-review-id"
        return [row]

    monkeypatch.setattr(llm_filter, "_call_ollama", malformed_ollama)

    kept, note = llm_filter.apply_llm_filter(
        profile,
        [match],
        True,
        "groq-model",
        1,
        "groq",
        True,
        1,
        rejected_audit=rejected,
        manual_review_audit=manual_review,
    )

    assert ollama_calls == ["1", "1"]
    assert rejected == []
    assert kept == []
    assert [item.job.source_id for item in manual_review] == ["1"]
    assert manual_review[0].llm_decision == "review_failed"
    assert manual_review[0].llm_reason.startswith("Manual review required:")
    assert "1 job(s) require manual review" in note


def test_ollama_failure_message_does_not_leak_provider_bodies(monkeypatch):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    match = MatchResult(
        Job(
            "test",
            "1",
            "Engineer",
            "Co",
            "Colombo, Sri Lanka",
            "sri lanka",
            "https://e/1",
            "Engineering role.",
        ),
        50,
        (),
        (),
    )
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "1")
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )
    monkeypatch.setattr(
        llm_filter,
        "_call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HTTPError(
                "url",
                429,
                "limited",
                {},
                BytesIO(b'{"error":"groq-private-detail"}'),
            )
        ),
    )
    monkeypatch.setattr(
        llm_filter,
        "_call_ollama",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HTTPError(
                "url",
                500,
                "failed",
                {},
                BytesIO(b'{"error":"ollama-private-detail"}'),
            )
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        llm_filter.apply_llm_filter(
            profile,
            [match],
            True,
            "groq-model",
            1,
            "groq",
            True,
            1,
            rejected_audit=[],
        )

    message = str(caught.value)
    assert "Required local Ollama fallback failed" in message
    assert "HTTP 500" in message
    assert "groq-private-detail" not in message
    assert "ollama-private-detail" not in message


def test_native_ollama_payload_uses_schema_context_and_keep_alive(monkeypatch):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    match = MatchResult(
        Job(
            "test",
            "1",
            "Engineer",
            "Co",
            "Colombo, Sri Lanka",
            "sri lanka",
            "https://e/1",
            "Engineering role.",
        ),
        50,
        (),
        (),
    )
    captured = {}
    monkeypatch.setenv("OLLAMA_CONTEXT_SIZE", "8192")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "512")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "10m")

    def fake_post(endpoint, body):
        captured["endpoint"] = endpoint
        captured["body"] = body
        return _keep_decisions([match])

    monkeypatch.setattr(llm_filter, "_post_ollama_chat", fake_post)

    llm_filter._call_ollama(
        profile,
        [match],
        "http://127.0.0.1:11434",
        "llama3.1:8b",
    )

    body = captured["body"]
    assert body["stream"] is False
    assert body["format"]["properties"]["jobs"]["minItems"] == 1
    assert body["format"]["properties"]["jobs"]["maxItems"] == 1
    assert body["options"]["num_ctx"] == 8192
    assert body["options"]["num_predict"] == 512
    assert body["keep_alive"] == "10m"


def test_ollama_warm_uses_review_context_and_skips_recent_duplicate(
    monkeypatch,
):
    requests = []
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_CONTEXT_SIZE", "8192")
    monkeypatch.setattr(llm_filter, "_OLLAMA_LAST_WARM_KEY", None)
    monkeypatch.setattr(llm_filter, "_OLLAMA_LAST_WARM_AT", 0.0)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr(llm_filter, "urlopen", fake_urlopen)

    llm_filter.warm_ollama_fallback()
    llm_filter.warm_ollama_fallback()

    assert len(requests) == 1
    assert requests[0]["options"]["num_ctx"] == 8192


def test_ollama_uses_compact_candidate_evidence_and_id_only_schema(monkeypatch):
    profile = CandidateProfile(
        "A" * 5000,
        skills=("python", "aws"),
        likely_titles=("devops engineer",),
        experience_lines=("DevOps Engineer - 2 years",),
        target_position="DevOps Engineer",
        experience_years=2,
    )
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "DevOps Engineer",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://private.example/jobs/{index}",
                "Operate AWS infrastructure. " * 200,
            ),
            70,
            ("aws",),
            ("devops",),
        )
        for index in range(2)
    ]
    captured = {}
    monkeypatch.setenv("OLLAMA_CV_CHAR_LIMIT", "1200")
    monkeypatch.setenv("OLLAMA_JOB_DESCRIPTION_CHAR_LIMIT", "700")

    def fake_post(endpoint, body):
        captured["body"] = body
        return _keep_decisions(matches)

    monkeypatch.setattr(llm_filter, "_post_ollama_chat", fake_post)

    llm_filter._call_ollama(
        profile,
        matches,
        "http://127.0.0.1:11434",
        "llama3.1:8b",
        country="sri lanka",
    )

    body = captured["body"]
    prompt = json.loads(body["messages"][1]["content"])
    assert prompt["candidate"]["evidence_mode"] == "compact_structured"
    assert len(prompt["candidate"]["cv_text"]) == 1200
    assert all("url" not in row and "source_id" not in row for row in prompt["jobs"])
    item_schema = body["format"]["properties"]["jobs"]["items"]
    assert "url" not in item_schema["properties"]
    assert "url" not in item_schema["required"]
    assert item_schema["properties"]["review_id"]["enum"] == [
        llm_filter._review_id(match) for match in matches
    ]
    assert body["format"]["properties"]["jobs"]["minItems"] == 2
    assert body["format"]["properties"]["jobs"]["maxItems"] == 2


def test_compact_ollama_prompt_bounds_every_candidate_and_job_text(monkeypatch):
    long_text = "evidence-" + ("x" * 10000)
    profile = CandidateProfile(
        long_text,
        skills=tuple(long_text for _ in range(61)),
        likely_titles=tuple(long_text for _ in range(21)),
        experience_lines=tuple(long_text for _ in range(17)),
        target_position=long_text,
        experience_years=2,
    )
    match = MatchResult(
        Job(
            long_text,
            long_text,
            long_text,
            long_text,
            long_text,
            long_text,
            "https://example.invalid/job",
            long_text,
            published_at=long_text,
            salary=long_text,
            job_type=long_text,
        ),
        70,
        (),
        (),
        concerns=tuple(long_text for _ in range(17)),
    )
    monkeypatch.setenv("OLLAMA_CV_CHAR_LIMIT", "1200")
    monkeypatch.setenv("OLLAMA_JOB_DESCRIPTION_CHAR_LIMIT", "700")

    messages, _ = llm_filter._build_review_request(
        profile,
        [match],
        country=long_text,
        compact_candidate=True,
        include_url=False,
    )

    prompt = json.loads(messages[1]["content"])
    candidate = prompt["candidate"]
    assert len(candidate["target_position"]) == 200
    assert len(candidate["target_country"]) == 120
    assert len(candidate["skills"]) == 60
    assert all(len(value) == 96 for value in candidate["skills"])
    assert len(candidate["likely_titles"]) == 20
    assert all(len(value) == 180 for value in candidate["likely_titles"])
    assert len(candidate["experience_lines"]) == 16
    assert all(len(value) == 220 for value in candidate["experience_lines"])
    assert len(candidate["cv_text"]) == 1200

    job = prompt["jobs"][0]
    assert "source_id" not in job
    assert "url" not in job
    assert len(job["title"]) == 240
    assert len(job["company"]) == 180
    assert len(job["location"]) == 180
    assert len(job["country_hint"]) == 120
    assert len(job["source"]) == 100
    assert len(job["description"]) == 700
    assert len(job["published_at"]) == 80
    assert len(job["salary"]) == 160
    assert len(job["job_type"]) == 100
    assert len(job["concerns"]) == 16
    assert all(len(value) == 240 for value in job["concerns"])


def test_checkpoint_context_tracks_rendered_prompt_and_schema_policy(monkeypatch):
    profile = CandidateProfile(
        "CV",
        target_position="Engineer",
        experience_years=2,
    )
    kwargs = {
        "requested_provider": "groq",
        "primary_provider": "Groq",
        "primary_model": "model",
        "fallback_enabled": True,
        "fallback_model": "llama3.1:8b",
        "strict": True,
    }
    original_builder = llm_filter._build_review_request
    original_context = llm_filter._review_checkpoint_context(
        profile,
        "sri lanka",
        False,
        **kwargs,
    )

    def changed_builder(*args, **builder_kwargs):
        messages, schema = original_builder(*args, **builder_kwargs)
        messages = [dict(message) for message in messages]
        messages[0]["content"] += " Revised policy."
        return messages, schema

    monkeypatch.setattr(llm_filter, "_build_review_request", changed_builder)
    changed_context = llm_filter._review_checkpoint_context(
        profile,
        "sri lanka",
        False,
        **kwargs,
    )

    assert changed_context != original_context


def test_invalid_two_row_ollama_batch_falls_back_to_exact_singletons(
    monkeypatch,
):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Engineer",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://e/{index}",
                "Engineering role.",
            ),
            50,
            (),
            (),
        )
        for index in range(2)
    ]
    calls = []
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("OLLAMA_BATCH_SIZE", "2")
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )
    monkeypatch.setattr(
        llm_filter,
        "_call_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    def local_call(profile, batch, *args, **kwargs):
        calls.append([item.job.source_id for item in batch])
        rows = _keep_decisions(batch)
        if len(batch) == 2:
            rows[0]["review_id"] = "wrong"
        return rows

    monkeypatch.setattr(llm_filter, "_call_ollama", local_call)

    kept, _ = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "model",
        1,
        "groq",
        True,
        2,
        rejected_audit=[],
        manual_review_audit=[],
    )

    assert calls == [["0", "1"], ["0"], ["1"]]
    assert [item.job.source_id for item in kept] == ["0", "1"]


def test_checkpoint_restores_completed_rows_after_later_provider_failure(
    monkeypatch,
    tmp_path,
):
    profile = CandidateProfile("CV", target_position="Engineer", experience_years=2)
    matches = [
        MatchResult(
            Job(
                "test",
                str(index),
                "Engineer",
                "Co",
                "Colombo, Sri Lanka",
                "sri lanka",
                f"https://e/{index}",
                "Engineering role.",
            ),
            50,
            (),
            (),
        )
        for index in range(2)
    ]
    checkpoint = tmp_path / "llm_review_checkpoint.jsonl"
    first_calls = []
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(
        llm_filter,
        "_resolve_llm_config",
        lambda model, provider: ("Groq", "key", "url", model),
    )

    def interrupted_call(profile, batch, *args, **kwargs):
        first_calls.append(batch[0].job.source_id)
        if batch[0].job.source_id == "1":
            raise URLError("offline")
        return _keep_decisions(batch)

    monkeypatch.setattr(llm_filter, "_call_llm", interrupted_call)
    completed = []
    with pytest.raises(RuntimeError, match="connection failure"):
        llm_filter.apply_llm_filter(
            profile,
            matches,
            True,
            "model",
            1,
            "groq",
            True,
            1,
            completed_audit=completed,
            checkpoint_path=checkpoint,
        )

    assert first_calls == ["0", "1"]
    assert [item.job.source_id for item in completed] == ["0"]
    assert len(checkpoint.read_text(encoding="utf-8").splitlines()) == 1

    resumed_calls = []

    def resumed_call(profile, batch, *args, **kwargs):
        resumed_calls.append(batch[0].job.source_id)
        return _keep_decisions(batch)

    monkeypatch.setattr(llm_filter, "_call_llm", resumed_call)
    resumed_completed = []
    kept, note = llm_filter.apply_llm_filter(
        profile,
        matches,
        True,
        "model",
        1,
        "groq",
        True,
        1,
        completed_audit=resumed_completed,
        checkpoint_path=checkpoint,
    )

    assert resumed_calls == ["1"]
    assert [item.job.source_id for item in kept] == ["0", "1"]
    assert [item.job.source_id for item in resumed_completed] == ["0", "1"]
    assert "resumed 1 checkpointed review(s)" in note

    changed_matches = [
        replace(
            matches[0],
            job=replace(
                matches[0].job,
                description="Changed requirements at the same vacancy URL.",
            ),
        ),
        matches[1],
    ]
    changed_calls = []

    def changed_call(profile, batch, *args, **kwargs):
        changed_calls.extend(item.job.source_id for item in batch)
        return _keep_decisions(batch)

    monkeypatch.setattr(llm_filter, "_call_llm", changed_call)
    kept, _ = llm_filter.apply_llm_filter(
        profile,
        changed_matches,
        True,
        "model",
        1,
        "groq",
        True,
        1,
        checkpoint_path=checkpoint,
    )

    assert changed_calls == ["0"]
    assert {item.job.source_id for item in kept} == {"0", "1"}

    changed_model_calls = []

    def changed_model_call(profile, batch, *args, **kwargs):
        changed_model_calls.extend(item.job.source_id for item in batch)
        return _keep_decisions(batch)

    monkeypatch.setattr(llm_filter, "_call_llm", changed_model_call)
    llm_filter.apply_llm_filter(
        profile,
        changed_matches,
        True,
        "model-v2",
        1,
        "groq",
        True,
        1,
        checkpoint_path=checkpoint,
    )

    assert changed_model_calls == ["0", "1"]


def test_json_parser_accepts_fenced_or_prefixed_valid_json():
    assert llm_filter._parse_decision_content(
        '```json\n{"jobs":[{"review_id":"one"}]}\n```'
    ) == [{"review_id": "one"}]
    assert llm_filter._parse_decision_content(
        'Result follows: {"jobs":[{"review_id":"two"}]}'
    ) == [{"review_id": "two"}]


def _keep_decisions(batch):
    return [
        {
            "review_id": llm_filter._review_id(item),
            "url": item.job.url,
            "decision": "keep",
            "score": 80,
            "reason": "fit",
        }
        for item in batch
    ]
