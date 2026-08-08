from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .job_sources import AdzunaProvider, JobProvider, default_providers
from .it_company_sources import is_it_position, it_company_career_providers
from .models import CandidateProfile, Job

logger = logging.getLogger(__name__)

SourceAgentStatus = Literal[
    "completed_with_results",
    "completed_inventory_no_role_candidates",
    "connector_empty_unverified",
    "skipped",
    "failed",
]


@dataclass(frozen=True)
class SourceAgentTrace:
    """Auditable result produced by one vertical source-agent node."""

    source: str
    connector: str
    status: SourceAgentStatus
    discovered: int = 0
    eligible: int = 0
    inventory_total: int | None = None
    note: str = ""


@dataclass
class AgentGraphState:
    """State passed from the root node through every source node."""

    profile: CandidateProfile
    country: str
    jobs: list[Job] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    traces: list[SourceAgentTrace] = field(default_factory=list)


@dataclass(frozen=True)
class AgentGraphOptions:
    limit_per_source: int = 200
    include_remote_global: bool = False
    web_discovery: bool = False
    minimum_score: float = 0.0
    llm_enabled: bool = False
    llm_model: str = "gpt-4.1-mini"
    llm_provider: str = "auto"
    llm_strict: bool = False
    llm_batch_size: int = 5


@dataclass(frozen=True)
class _DiscoveryCacheEntry:
    """Immutable fan-in snapshot shared only by equivalent discovery requests."""

    jobs: tuple[Job, ...]
    notes: tuple[str, ...]
    traces: tuple[SourceAgentTrace, ...]
    stored_at: float
    captured_at: datetime


_DISCOVERY_RESULT_CACHE: OrderedDict[str, _DiscoveryCacheEntry] = OrderedDict()
_DISCOVERY_RESULT_FLIGHTS: dict[str, "_DiscoveryResultFlight"] = {}
_DISCOVERY_RESULT_CACHE_LOCK = threading.RLock()


class _DiscoveryResultFlight:
    """A minimal Future-like result used to coalesce identical live crawls."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._entry: _DiscoveryCacheEntry | None = None
        self._error: BaseException | None = None

    def finish(self, entry: _DiscoveryCacheEntry) -> None:
        self._entry = entry
        self._event.set()

    def fail(self, error: BaseException) -> None:
        self._error = error
        self._event.set()

    def result(self) -> _DiscoveryCacheEntry:
        self._event.wait()
        if self._error is not None:
            raise self._error
        if self._entry is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Discovery cache single-flight completed without a result")
        return self._entry


class SourceAgent:
    """One discovery-only graph node responsible for exactly one provider."""

    def __init__(self, provider: JobProvider, options: AgentGraphOptions) -> None:
        self.provider = provider
        self.options = options

    def run(self, state: AgentGraphState) -> AgentGraphState:
        skip_reason = self._skip_reason()
        connector = self._connector_name()
        if skip_reason:
            state.notes.append(f"{self.provider.name}: skipped ({skip_reason})")
            state.traces.append(
                SourceAgentTrace(self.provider.name, connector, "skipped", note=skip_reason)
            )
            return state

        logger.info("Agent node %s: starting via %s", self.provider.name, connector)
        try:
            discovered = self.provider.search(
                state.profile, state.country, self.options.limit_per_source
            )
            state.jobs.extend(discovered)
            inventory_total = getattr(self.provider, "last_inventory_count", None)
            hit_limit = bool(
                self.options.limit_per_source > 0
                and len(discovered) >= self.options.limit_per_source
            )
            if discovered:
                status: SourceAgentStatus = "completed_with_results"
                trace_note = (
                    "configured result cap reached; additional role candidates may exist"
                    if hit_limit
                    else ""
                )
            elif inventory_total is not None:
                status = "completed_inventory_no_role_candidates"
                trace_note = (
                    f"complete current inventory loaded ({inventory_total} rows); "
                    "deterministic role-keyword gate returned no candidates"
                )
            else:
                status = "connector_empty_unverified"
                trace_note = "connector returned no rows; website inventory not verified empty"
            detail = f"{self.provider.name}: {len(discovered)} discovered"
            if trace_note:
                detail += f" ({trace_note})"
            state.notes.append(detail)
            state.traces.append(
                SourceAgentTrace(
                    self.provider.name,
                    connector,
                    status,
                    discovered=len(discovered),
                    eligible=len(discovered),
                    inventory_total=inventory_total,
                    note=trace_note,
                )
            )
        except (Exception,) as exc:
            # Source isolation is intentional: a blocked or changed portal must
            # not prevent later nodes from running.
            logger.warning("Agent node %s failed: %s", self.provider.name, exc)
            state.notes.append(f"{self.provider.name}: failed: {exc}")
            state.traces.append(
                SourceAgentTrace(self.provider.name, connector, "failed", note=str(exc))
            )
        return state

    def _skip_reason(self) -> str:
        if isinstance(self.provider, AdzunaProvider) and not self.provider.enabled:
            return "credentials not configured"
        if self.provider.is_search_discovery and not self.options.web_discovery:
            return "web discovery disabled"
        if self.provider.disabled_reason:
            return self.provider.disabled_reason
        if self.provider.is_remote_global and not self.options.include_remote_global:
            return "remote-global sources disabled"
        return ""

    def _connector_name(self) -> str:
        name = type(self.provider).__name__
        if name in {"SriLankaPortalProvider", "Crawl4AiSeedProvider"}:
            return "crawl4ai/html"
        if "Provider" in name:
            return "official-api/rss/html"
        return name


class VerticalJobAgentGraph:
    """Root profile -> concurrent source fan-out -> deterministic finalizer."""

    def __init__(
        self,
        options: AgentGraphOptions,
        providers: list[JobProvider] | None = None,
    ) -> None:
        self._uses_default_providers = providers is None
        self.options = options
        self.providers = providers if providers is not None else default_providers()
        # Capture constructor/configuration state before providers add runtime
        # fields such as last_inventory_count. This makes separate graph
        # instances with the same source configuration shareable.
        self._provider_cache_signature = _provider_signature(self.providers)

    def run(self, profile: CandidateProfile, country: str) -> AgentGraphState:
        # Providers currently use target_position, likely_titles, and skills,
        # and custom providers may legitimately consult other profile fields.
        # Preserve the complete source-query behavior; the cache key includes
        # the complete profile so candidate-specific discovery never leaks
        # across users.
        discovery_profile = profile
        ttl_seconds, max_entries = _discovery_cache_settings()
        if ttl_seconds <= 0 or max_entries <= 0:
            state = self._run_uncached(discovery_profile, country)
            return _copy_discovery_state(state, profile, country)

        cache_key = _discovery_cache_key(
            discovery_profile,
            country,
            self.options,
            self._provider_cache_signature,
        )
        now = time.monotonic()
        with _DISCOVERY_RESULT_CACHE_LOCK:
            _prune_discovery_result_cache(now, ttl_seconds)
            cached = _DISCOVERY_RESULT_CACHE.get(cache_key)
            if cached is not None:
                _DISCOVERY_RESULT_CACHE.move_to_end(cache_key)
                return _state_from_cache_entry(
                    cached,
                    profile,
                    country,
                    cache_event="hit",
                    now=now,
                )
            flight = _DISCOVERY_RESULT_FLIGHTS.get(cache_key)
            if flight is None:
                flight = _DiscoveryResultFlight()
                _DISCOVERY_RESULT_FLIGHTS[cache_key] = flight
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            logger.info(
                "Discovery result cache: waiting for identical in-progress crawl "
                "(role=%s, country=%s)",
                discovery_profile.target_position or "(CV-derived)",
                country,
            )
            entry = flight.result()
            return _state_from_cache_entry(
                entry,
                profile,
                country,
                cache_event="single-flight reuse",
                now=time.monotonic(),
            )

        try:
            state = self._run_uncached(discovery_profile, country)
            stored_at = time.monotonic()
            entry = _DiscoveryCacheEntry(
                jobs=tuple(state.jobs),
                notes=tuple(state.notes),
                traces=tuple(state.traces),
                stored_at=stored_at,
                captured_at=datetime.now(timezone.utc),
            )
            with _DISCOVERY_RESULT_CACHE_LOCK:
                _DISCOVERY_RESULT_CACHE[cache_key] = entry
                _DISCOVERY_RESULT_CACHE.move_to_end(cache_key)
                while len(_DISCOVERY_RESULT_CACHE) > max_entries:
                    _DISCOVERY_RESULT_CACHE.popitem(last=False)
                _DISCOVERY_RESULT_FLIGHTS.pop(cache_key, None)
                flight.finish(entry)
            result = _state_from_cache_entry(
                entry,
                profile,
                country,
                cache_event="refreshed",
                now=stored_at,
            )
            return result
        except BaseException as exc:
            with _DISCOVERY_RESULT_CACHE_LOCK:
                _DISCOVERY_RESULT_FLIGHTS.pop(cache_key, None)
                flight.fail(exc)
            raise

    def _run_uncached(
        self,
        discovery_profile: CandidateProfile,
        country: str,
    ) -> AgentGraphState:
        state = AgentGraphState(profile=discovery_profile, country=country)
        run_providers = list(self.providers)
        if self._uses_default_providers and is_it_position(discovery_profile.target_position):
            run_providers.extend(it_company_career_providers())
            state.notes.append(
                "IT career-page routing: activated 100 Sri Lankan technology-company sources"
            )
        logger.info(
            "Root agent: profile extracted; dispatching %d concurrent source nodes",
            len(run_providers),
        )
        workers = min(
            len(run_providers),
            max(1, int(os.getenv("SOURCE_AGENT_WORKERS", "8"))),
        )
        results: dict[int, AgentGraphState] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="source-agent") as pool:
            futures = {
                pool.submit(
                    SourceAgent(provider, self.options).run,
                    AgentGraphState(profile=discovery_profile, country=country),
                ): index
                for index, provider in enumerate(run_providers)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        # Merge in configured provider order so CSVs and audits are reproducible.
        for index in range(len(run_providers)):
            result = results[index]
            state.jobs.extend(result.jobs)
            state.notes.extend(result.notes)
            state.traces.extend(result.traces)
        state.jobs = _dedupe_jobs(state.jobs)
        completed_with_results = sum(
            trace.status == "completed_with_results" for trace in state.traces
        )
        empty_unverified = sum(
            trace.status == "connector_empty_unverified" for trace in state.traces
        )
        inventory_no_candidates = sum(
            trace.status == "completed_inventory_no_role_candidates"
            for trace in state.traces
        )
        skipped = sum(trace.status == "skipped" for trace in state.traces)
        failed = sum(trace.status == "failed" for trace in state.traces)
        state.notes.append(
            f"Discovery fan-in: {len(state.jobs)} unique raw jobs; "
            f"{completed_with_results} source connector(s) returned results, "
            f"{inventory_no_candidates} loaded complete inventories with no role candidates, "
            f"{empty_unverified} returned no rows (inventory unverified), "
            f"{skipped} skipped, {failed} failed"
        )
        logger.info("Discovery fan-in: %d unique raw jobs", len(state.jobs))
        return state

    def manifest(self) -> str:
        """Return a machine-readable graph manifest for diagnostics/UI work."""
        return json.dumps(
            {
                "direction": "fan-out/fan-in",
                "root": "cv_extraction_agent",
                "source_agents": [
                    {"order": index, "source": provider.name}
                    for index, provider in enumerate(self.providers, start=1)
                ],
                "final": "keyword_filter_then_single_llm_agent",
            },
            indent=2,
        )


def _discovery_cache_settings() -> tuple[float, int]:
    try:
        ttl_minutes = float(
            os.getenv("DISCOVERY_RESULT_CACHE_MINUTES", "10").strip()
        )
    except ValueError:
        logger.warning(
            "Invalid DISCOVERY_RESULT_CACHE_MINUTES; using the 10 minute default"
        )
        ttl_minutes = 10.0
    if not math.isfinite(ttl_minutes):
        logger.warning(
            "Non-finite DISCOVERY_RESULT_CACHE_MINUTES; using the 10 minute default"
        )
        ttl_minutes = 10.0
    try:
        max_entries = int(
            os.getenv("DISCOVERY_RESULT_CACHE_MAX_ENTRIES", "32").strip()
        )
    except ValueError:
        logger.warning(
            "Invalid DISCOVERY_RESULT_CACHE_MAX_ENTRIES; using the 32 entry default"
        )
        max_entries = 32
    # Guard against an accidental unbounded retention configuration.
    return min(max(0.0, ttl_minutes), 1440.0) * 60.0, min(
        max(0, max_entries),
        1024,
    )


def _provider_signature(providers: list[JobProvider]) -> tuple[object, ...]:
    """Return stable constructor/configuration identities for source agents."""
    return tuple(
        (
            type(provider).__module__,
            type(provider).__qualname__,
            provider.name,
            bool(provider.is_remote_global),
            bool(provider.is_search_discovery),
            _freeze_cache_value(vars(provider)),
        )
        for provider in providers
    )


def _freeze_cache_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return tuple(
            (str(key), _freeze_cache_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_cache_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(
            sorted((_freeze_cache_value(item) for item in value), key=repr)
        )
    # Custom test/deployment providers can opt into stable sharing by keeping
    # constructor fields serializable. The fallback remains safe (a memory
    # address merely causes a cache miss, never an incorrect cache hit).
    return repr(value)


_CACHE_CONTROL_ENVIRONMENT = {
    "DISCOVERY_RESULT_CACHE_MAX_ENTRIES",
    "DISCOVERY_RESULT_CACHE_MINUTES",
}


def _discovery_cache_key(
    profile: CandidateProfile,
    country: str,
    options: AgentGraphOptions,
    provider_signature: tuple[object, ...],
) -> str:
    # Providers consult environment variables at run time. Hashing the whole
    # environment ensures a credential, endpoint, Crawl4AI, query-cap, or other
    # connector setting change cannot receive an entry produced under an older
    # configuration. Cache-control variables themselves do not change source
    # results and are intentionally excluded.
    environment = tuple(
        sorted(
            (key, value)
            for key, value in os.environ.items()
            if key not in _CACHE_CONTROL_ENVIRONMENT
        )
    )
    payload = (
        (
            profile.raw_text,
            profile.name,
            profile.email,
            profile.phone,
            profile.links,
            profile.skills,
            profile.likely_titles,
            profile.experience_lines,
            profile.target_position,
            profile.experience_years,
        ),
        country,
        options.limit_per_source,
        options.include_remote_global,
        options.web_discovery,
        provider_signature,
        environment,
    )
    # Only the digest is retained, so credentials and other environment values
    # never appear in cache metadata, notes, or logs.
    encoded = json.dumps(
        _freeze_cache_value(payload),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prune_discovery_result_cache(now: float, ttl_seconds: float) -> None:
    expired = [
        key
        for key, entry in _DISCOVERY_RESULT_CACHE.items()
        if now - entry.stored_at > ttl_seconds
    ]
    for key in expired:
        _DISCOVERY_RESULT_CACHE.pop(key, None)


def _copy_discovery_state(
    state: AgentGraphState,
    profile: CandidateProfile,
    country: str,
) -> AgentGraphState:
    return AgentGraphState(
        profile=profile,
        country=country,
        jobs=list(state.jobs),
        notes=list(state.notes),
        traces=list(state.traces),
    )


def _state_from_cache_entry(
    entry: _DiscoveryCacheEntry,
    profile: CandidateProfile,
    country: str,
    *,
    cache_event: str,
    now: float,
) -> AgentGraphState:
    age_seconds = max(0.0, now - entry.stored_at)
    state = AgentGraphState(
        profile=profile,
        country=country,
        jobs=list(entry.jobs),
        notes=list(entry.notes),
        traces=list(entry.traces),
    )
    cache_note = (
        "Discovery result cache: "
        f"{cache_event}; reused {len(entry.traces)} auditable source trace(s); "
        f"captured {age_seconds:.1f}s ago at "
        f"{entry.captured_at.isoformat(timespec='seconds')}"
    )
    # Preserve the established invariant that the fan-in audit summary is the
    # final graph note while still making cache reuse explicit in every report.
    insert_at = (
        len(state.notes) - 1
        if state.notes and state.notes[-1].startswith("Discovery fan-in:")
        else len(state.notes)
    )
    state.notes.insert(insert_at, cache_note)
    logger.info(
        "Discovery result cache: %s (%d jobs, %d source traces, age %.1fs)",
        cache_event,
        len(entry.jobs),
        len(entry.traces),
        age_seconds,
    )
    return state


def _clear_discovery_result_cache_for_tests() -> None:
    """Reset process-local cache state for isolated tests."""
    with _DISCOVERY_RESULT_CACHE_LOCK:
        _DISCOVERY_RESULT_CACHE.clear()
        _DISCOVERY_RESULT_FLIGHTS.clear()


def _dedupe_jobs(jobs: list[Job]) -> list[Job]:
    # First collapse URL variants. Keep query parameters that can identify a
    # vacancy, but discard common advertising/click-tracking parameters.
    by_url: dict[str, Job] = {}
    for job in jobs:
        key = _canonical_job_url(job.url) or _fallback_job_identity(job)
        current = by_url.get(key)
        if current is None or _description_richness(job) > _description_richness(current):
            by_url[key] = job

    # Title and company alone are not a safe vacancy identity: an employer may
    # legitimately have multiple requisitions for one role. Consolidate
    # cross-URL copies only when location and substantive description agree.
    identity_indexes: dict[str, list[int]] = {}
    unique: list[Job] = []
    for job in by_url.values():
        identity = _job_identity(job)
        if not identity:
            unique.append(job)
            continue
        duplicate_index = next(
            (
                index
                for index in identity_indexes.get(identity, [])
                if _likely_same_vacancy(unique[index], job)
            ),
            None,
        )
        if duplicate_index is None:
            identity_indexes.setdefault(identity, []).append(len(unique))
            unique.append(job)
        elif _description_richness(job) > _description_richness(unique[duplicate_index]):
            unique[duplicate_index] = job
    return unique


_TRACKING_QUERY_PARAMETERS = {
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}


def _canonical_job_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.casefold()
    if not parsed.netloc:
        return raw.casefold()
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_PARAMETERS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path,
            urlencode(sorted(query)),
            "",
        )
    )


def _job_identity(job: Job) -> str:
    title = _normalize_identity_part(job.title)
    company = _normalize_identity_part(job.company)
    anonymous_companies = {
        "anonymous",
        "company confidential",
        "confidential",
        "not disclosed",
        "not specified",
        "unknown",
    }
    if not title or not company or company in anonymous_companies:
        return ""
    return f"{title}|{company}"


def _fallback_job_identity(job: Job) -> str:
    return "|".join(
        _normalize_identity_part(value)
        for value in (
            job.source,
            job.source_id,
            job.title,
            job.company,
            job.location,
            job.published_at,
            job.description,
        )
    )


def _normalize_identity_part(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _likely_same_vacancy(left: Job, right: Job) -> bool:
    if (
        left.source.casefold() == right.source.casefold()
        and left.source_id.casefold() != right.source_id.casefold()
    ):
        return False
    left_description = _normalize_identity_part(left.description)
    right_description = _normalize_identity_part(right.description)
    if min(len(left_description), len(right_description)) < 80:
        return False

    left_location = _normalize_identity_part(left.location)
    right_location = _normalize_identity_part(right.location)
    location_agrees = (
        not left_location
        or not right_location
        or left_location == right_location
        or left_location in right_location
        or right_location in left_location
    )
    if not location_agrees:
        return False

    left_tokens = set(left_description.split())
    right_tokens = set(right_description.split())
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / min(
        len(left_tokens), len(right_tokens)
    )
    return overlap >= 0.8


def _description_richness(job: Job) -> tuple[int, int]:
    description = " ".join(job.description.split())
    return (len(description), len(job.title.strip()) + len(job.company.strip()))
