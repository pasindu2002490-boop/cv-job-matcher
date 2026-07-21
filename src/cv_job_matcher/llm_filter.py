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
    provider: str = "auto",
    strict: bool = False,
    batch_size: int = 15,
) -> tuple[list[MatchResult], str]:
    if not enabled:
        return matches, ""
    provider_name, api_key, endpoint, selected_model = _resolve_llm_config(model, provider)
    if not api_key:
        if strict:
            raise RuntimeError(f"{provider} LLM filtering is required but its API key is not configured")
        return matches, "LLM filter: skipped (OPENAI_API_KEY or GROQ_API_KEY is not configured)"
    # Strict mode is the website's final eligibility gate, so every result from
    # the normal matcher must be reviewed. The limit remains available only for
    # optional/non-strict CLI use.
    selected = matches if strict else matches[: max(1, limit)]

    reranked = []
    blocked_jobs = 0
    batch_size = max(1, batch_size)
    for start in range(0, len(selected), batch_size):
        pending = [selected[start : start + batch_size]]
        while pending:
            batch = pending.pop(0)
            try:
                decisions = _call_llm(profile, batch, api_key, endpoint, selected_model)
            except HTTPError as exc:
                # Groq/WAF can reject content from a particular listing. Split
                # the batch to isolate it; a blocked single job fails closed.
                if exc.code == 403 and len(batch) > 1:
                    middle = len(batch) // 2
                    pending[0:0] = [batch[:middle], batch[middle:]]
                    continue
                if exc.code == 403 and len(batch) == 1:
                    blocked_jobs += 1
                    continue
                detail = _error_detail(exc)
                if strict:
                    raise RuntimeError(f"Required LLM filtering failed: {detail}") from exc
                return matches, f"LLM filter: failed, deterministic ranking kept ({detail})"
            except (URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                detail = _error_detail(exc)
                if strict:
                    raise RuntimeError(f"Required LLM filtering failed: {detail}") from exc
                return matches, f"LLM filter: failed, deterministic ranking kept ({detail})"
            by_url = {item.get("url", ""): item for item in decisions if isinstance(item, dict)}
            for match in batch:
                decision = by_url.get(match.job.url, {})
                verdict = str(decision.get("decision", "reject" if strict else "keep")).lower()
                if verdict == "reject" or (strict and verdict != "keep"):
                    continue
                score = decision.get("score")
                reason = str(decision.get("reason", "")).strip()
                safe_score = float(score) if isinstance(score, (int, float)) else match.score
                reranked.append(
                    replace(
                        match,
                        score=max(0.0, min(safe_score, 100.0)),
                        llm_decision=verdict,
                        llm_reason=reason,
                    )
                )
    kept_urls = {match.job.url for match in reranked}
    if not strict:
        reranked.extend(match for match in matches[len(selected) :] if match.job.url not in kept_urls)
    return (
        sorted(reranked, key=lambda item: item.score, reverse=True),
        f"LLM filter: reviewed {len(selected)} job(s) with {provider_name} / {selected_model}"
        + (f"; rejected {blocked_jobs} provider-blocked job(s)" if blocked_jobs else ""),
    )


def _resolve_llm_config(model: str, provider: str = "auto") -> tuple[str, str, str, str]:
    provider = provider.strip().lower()
    if provider not in {"auto", "openai", "groq"}:
        raise ValueError("LLM provider must be auto, openai, or groq")
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key and provider in {"auto", "openai"}:
        return (
            "OpenAI",
            openai_key,
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model,
        )

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key and provider in {"auto", "groq"}:
        groq_model = os.getenv("GROQ_MODEL", "").strip()
        selected_model = groq_model or (model if model != "gpt-4.1-mini" else "openai/gpt-oss-20b")
        return (
            "Groq",
            groq_key,
            os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            selected_model,
        )

    return provider.title() if provider != "auto" else "", "", "", model


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
            "description": match.job.description[:700],
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
            "cv_text": profile.raw_text[:6000],
        },
        "task": (
            "Act as a strict eligibility gate, not a recommendation assistant. The supplied experience_years "
            "is the candidate's maximum verified professional experience; never infer extra years from skills "
            "or the CV. REJECT any job whose title, duties, or stated minimum experience exceeds it. For a "
            "candidate below 3 years reject Senior/Sr/Lead roles; below 5 reject Staff/Principal/Manager/Architect; "
            "below 7 reject Director/Head/VP/Chief roles. REJECT jobs outside target_position, non-job/search pages, "
            "and unclear matches. KEEP only a clearly suitable, currently open role in the requested field. "
            "Use maybe only for genuine ambiguity; strict callers will exclude maybe. Return one result for every "
            "input URL and JSON only."
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
    return _post_chat_completion(endpoint, api_key, body)


def _error_detail(exc: Exception) -> str:
    if not isinstance(exc, HTTPError):
        return str(exc)
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = error.get("message") if isinstance(error, dict) else ""
        code = error.get("code") if isinstance(error, dict) else ""
        if message:
            suffix = f" ({code})" if code else ""
            return f"Groq HTTP {exc.code}: {message}{suffix}"
    except (json.JSONDecodeError, OSError):
        pass
    return f"Groq HTTP {exc.code}: {exc.reason}"


def _post_chat_completion(endpoint: str, api_key: str, body: dict) -> list[dict]:
    request = Request(
        f"{endpoint}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # urllib's default signature is blocked by Groq's Cloudflare rules
            # (Error 1010) on some Windows connections.
            "User-Agent": "cv-job-matcher/0.1",
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
