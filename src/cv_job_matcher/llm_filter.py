from __future__ import annotations

import json
import os
from dataclasses import replace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import CandidateProfile, MatchResult


def apply_llm_filter(
    profile: CandidateProfile,
    matches: list[MatchResult],
    enabled: bool,
    model: str,
    limit: int,
) -> tuple[list[MatchResult], str]:
    if not enabled:
        return matches, ""
    provider, api_key, endpoint, selected_model = _resolve_llm_config(model)
    if not api_key:
        return matches, "LLM filter: skipped (OPENAI_API_KEY or GROQ_API_KEY is not configured)"
    selected = matches[: max(1, limit)]
    try:
        decisions = _call_llm(profile, selected, api_key, endpoint, selected_model)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return matches, f"LLM filter: failed, deterministic ranking kept ({exc})"

    by_url = {item.get("url", ""): item for item in decisions if isinstance(item, dict)}
    reranked = []
    for match in selected:
        decision = by_url.get(match.job.url, {})
        keep = str(decision.get("decision", "keep")).lower()
        if keep == "reject":
            continue
        score = decision.get("score")
        reason = str(decision.get("reason", "")).strip()
        reranked.append(
            replace(
                match,
                score=float(score) if isinstance(score, (int, float)) else match.score,
                llm_decision=keep,
                llm_reason=reason,
            )
        )
    kept_urls = {match.job.url for match in reranked}
    reranked.extend(match for match in matches[len(selected) :] if match.job.url not in kept_urls)
    return (
        sorted(reranked, key=lambda item: item.score, reverse=True),
        f"LLM filter: applied with {provider} / {selected_model}",
    )


def _resolve_llm_config(model: str) -> tuple[str, str, str, str]:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        return (
            "OpenAI",
            openai_key,
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model,
        )

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        groq_model = os.getenv("GROQ_MODEL", "").strip()
        selected_model = groq_model or (model if model != "gpt-4.1-mini" else "openai/gpt-oss-20b")
        return (
            "Groq",
            groq_key,
            os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            selected_model,
        )

    return "", "", "", model


def _call_llm(
    profile: CandidateProfile,
    matches: list[MatchResult],
    api_key: str,
    endpoint: str,
    model: str,
) -> list[dict]:
    jobs = [
        {
            "url": match.job.url,
            "title": match.job.title,
            "company": match.job.company,
            "location": match.job.location,
            "source": match.job.source,
            "description": match.job.description[:1800],
            "heuristic_score": match.score,
            "concerns": list(match.concerns),
        }
        for match in matches
    ]
    prompt = {
        "candidate": {
            "target_position": profile.target_position,
            "experience_years": profile.experience_years,
            "skills": list(profile.skills),
            "cv_excerpt": profile.raw_text[:4000],
        },
        "task": (
            "Filter and rerank jobs ONLY by CV fit, target position fit, and experience years. "
            "Reject jobs that clearly require more seniority than supplied experience, are not related "
            "to the target position, or are obviously not real/open job listings. Return JSON only."
        ),
        "jobs": jobs,
        "output_schema": [
            {"url": "same URL", "decision": "keep|maybe|reject", "score": 0, "reason": "short reason"}
        ],
    }
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You are a strict recruiting analyst. Return compact JSON only.",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
        ],
    }
    try:
        return _post_chat_completion(endpoint, api_key, body)
    except HTTPError:
        # Some OpenAI-compatible providers/models do not support JSON-mode. The
        # prompt still requires JSON, so retry once without the optional hint.
        fallback = dict(body)
        fallback.pop("response_format", None)
        return _post_chat_completion(endpoint, api_key, fallback)


def _post_chat_completion(endpoint: str, api_key: str, body: dict) -> list[dict]:
    request = Request(
        f"{endpoint}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    content = payload["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if isinstance(parsed, dict) and isinstance(parsed.get("jobs"), list):
        return parsed["jobs"]
    if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        return parsed["results"]
    if isinstance(parsed, list):
        return parsed
    raise ValueError("LLM response did not contain a jobs/results list")
