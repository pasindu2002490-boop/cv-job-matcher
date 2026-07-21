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

    def fake_call(profile, batch, api_key, endpoint, model):
        reviewed.extend(item.job.url for item in batch)
        return [
            {"url": item.job.url, "decision": "maybe" if item.job.source_id == "3" else "keep", "score": 80, "reason": "test"}
            for item in batch
        ]

    monkeypatch.setattr(llm_filter, "_call_llm", fake_call)
    kept, note = llm_filter.apply_llm_filter(profile, matches, True, "model", 10, "groq", True, 2)

    assert len(reviewed) == 5
    assert len(kept) == 4
    assert all(item.job.source_id != "3" for item in kept)
    assert "reviewed 5 job(s)" in note
