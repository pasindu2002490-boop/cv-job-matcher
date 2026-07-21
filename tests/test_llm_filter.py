from cv_job_matcher import llm_filter
from cv_job_matcher.models import CandidateProfile, Job, MatchResult
from io import BytesIO
from urllib.error import HTTPError


def test_strict_llm_filter_batches_every_job_and_drops_maybe(monkeypatch):
    profile = CandidateProfile("CV", target_position="AI Engineer", experience_years=1)
    matches = [
        MatchResult(Job("test", str(i), "AI Engineer", "Co", "Remote", "", f"https://e/{i}", ""), 50, (), ())
        for i in range(5)
    ]
    reviewed = []

    monkeypatch.setattr(llm_filter, "_resolve_llm_config", lambda model, provider: ("Groq", "key", "url", model))

    def fake_call(profile, batch, api_key, endpoint, model):
        reviewed.extend(item.job.url for item in batch)
        return [
            {"url": item.job.url, "decision": "maybe" if item.job.source_id == "3" else "keep", "score": 80, "reason": "test"}
            for item in batch
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", fake_call)
    # Strict mode reviews all jobs even when the optional CLI limit is lower.
    kept, note = llm_filter.apply_llm_filter(profile, matches, True, "model", 2, "groq", True, 2)

    assert len(reviewed) == 5
    assert len(kept) == 4
    assert all(item.job.source_id != "3" for item in kept)
    assert "reviewed 5 job(s)" in note


def test_403_job_is_isolated_and_rejected_without_failing_search(monkeypatch):
    profile = CandidateProfile("private CV text", target_position="AI Engineer", experience_years=1)
    matches = [
        MatchResult(Job("test", str(i), "AI Engineer", "Co", "Remote", "", f"https://e/{i}", "description"), 50, (), ())
        for i in range(3)
    ]
    monkeypatch.setattr(llm_filter, "_resolve_llm_config", lambda model, provider: ("Groq", "key", "url", model))

    def fake_call(profile, batch, api_key, endpoint, model):
        if any(item.job.source_id == "1" for item in batch):
            raise HTTPError("url", 403, "Forbidden", {}, BytesIO(b"Forbidden"))
        return [{"url": item.job.url, "decision": "keep", "score": 80, "reason": "fit"} for item in batch]

    monkeypatch.setattr(llm_filter, "_call_llm", fake_call)
    kept, note = llm_filter.apply_llm_filter(profile, matches, True, "model", 10, "groq", True, 3)

    assert [item.job.source_id for item in kept] == ["0", "2"]
    assert "provider-blocked job(s)" in note
