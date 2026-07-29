from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from .job_sources import (
    Crawl4AiSeedProvider,
    ITProSriLankaProvider,
    SRI_LANKA_PORTALS,
    SriLankaPortalProvider,
    TopJobsSriLankaProvider,
    XpressJobsSriLankaProvider,
)
from .models import CandidateProfile, Job

logger = logging.getLogger(__name__)


class InventoryCapability(str, Enum):
    FULL_INVENTORY = "full_inventory"
    BOUNDED_LISTING = "bounded_listing"
    ROLE_QUERY = "role_query"
    SEARCH_DISCOVERY = "search_discovery"


@dataclass(frozen=True)
class InventorySource:
    name: str
    capability: InventoryCapability
    complete_for_scope: bool
    scope: str
    collect: Callable[[], list[Job]]


@dataclass(frozen=True)
class SourceBatch:
    source: str
    capability: InventoryCapability
    complete_for_scope: bool
    scope: str
    jobs: tuple[Job, ...]
    elapsed_seconds: float
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return not self.error


def default_inventory_sources(
    *,
    bounded_limit: int | None = None,
) -> list[InventorySource]:
    """Return only sources that can run without a user's CV or target role."""
    if bounded_limit is None:
        bounded_limit = _bounded_int("INVENTORY_BOUNDED_LIMIT", 80, 1, 5000)
    empty_profile = CandidateProfile(raw_text="")
    sources: list[InventorySource] = [
        InventorySource(
            name=ITProSriLankaProvider.name,
            capability=InventoryCapability.FULL_INVENTORY,
            complete_for_scope=True,
            scope="all currently listed Sri Lankan vacancies",
            collect=ITProSriLankaProvider()._load_inventory,
        ),
        InventorySource(
            name=TopJobsSriLankaProvider.name,
            capability=InventoryCapability.FULL_INVENTORY,
            complete_for_scope=True,
            scope="all currently open Sri Lankan vacancies",
            collect=TopJobsSriLankaProvider()._load_inventory,
        ),
        InventorySource(
            name=XpressJobsSriLankaProvider.name,
            capability=InventoryCapability.FULL_INVENTORY,
            complete_for_scope=True,
            scope="all active non-foreign vacancies returned by the public API",
            collect=XpressJobsSriLankaProvider()._load_inventory,
        ),
    ]
    for name, seed_url in SRI_LANKA_PORTALS:
        provider = SriLankaPortalProvider(name, seed_url)
        sources.append(
            InventorySource(
                name=name,
                capability=InventoryCapability.BOUNDED_LISTING,
                complete_for_scope=False,
                scope=(
                    f"up to {bounded_limit} details found within the connector's "
                    "bounded public-page traversal"
                ),
                collect=lambda provider=provider: provider.search(
                    empty_profile,
                    "sri lanka",
                    bounded_limit,
                ),
            )
        )
    crawl4ai = Crawl4AiSeedProvider()
    if not crawl4ai.disabled_reason:
        sources.append(
            InventorySource(
                name=crawl4ai.name,
                capability=InventoryCapability.BOUNDED_LISTING,
                complete_for_scope=False,
                scope=(
                    f"up to {bounded_limit} details from configured rendered seed pages"
                ),
                collect=lambda: crawl4ai.search(
                    empty_profile,
                    "sri lanka",
                    bounded_limit,
                ),
            )
        )
    return sources


def collect_inventory(
    sources: Iterable[InventorySource] | None = None,
    *,
    max_workers: int | None = None,
) -> list[SourceBatch]:
    """Run independent source agents concurrently and isolate every failure."""
    selected = list(sources if sources is not None else default_inventory_sources())
    if not selected:
        return []
    if max_workers is None:
        max_workers = _bounded_int(
            "INVENTORY_SOURCE_WORKERS",
            8,
            1,
            32,
        )
    results: dict[int, SourceBatch] = {}
    worker_count = max(1, min(max_workers, len(selected)))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="inventory-source",
    ) as executor:
        futures = {
            executor.submit(_collect_one, source): index
            for index, source in enumerate(selected)
        }
        for future in as_completed(futures):
            index = futures[future]
            # _collect_one is defensive, but retaining this boundary prevents a
            # programming error in one connector from losing all source metrics.
            try:
                results[index] = future.result()
            except Exception as exc:  # pragma: no cover - last-resort isolation
                source = selected[index]
                logger.exception("Inventory source %s crashed", source.name)
                results[index] = SourceBatch(
                    source=source.name,
                    capability=source.capability,
                    complete_for_scope=source.complete_for_scope,
                    scope=source.scope,
                    jobs=(),
                    elapsed_seconds=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
    return [results[index] for index in range(len(selected))]


def _collect_one(source: InventorySource) -> SourceBatch:
    started = time.monotonic()
    logger.info(
        "Inventory source starting: source=%s capability=%s",
        source.name,
        source.capability.value,
    )
    try:
        jobs = tuple(source.collect())
        error = ""
    except Exception as exc:
        jobs = ()
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("Inventory source failed: source=%s error=%s", source.name, error)
    elapsed = time.monotonic() - started
    logger.info(
        "Inventory source finished: source=%s rows=%d elapsed_seconds=%.3f success=%s",
        source.name,
        len(jobs),
        elapsed,
        not error,
    )
    return SourceBatch(
        source=source.name,
        capability=source.capability,
        complete_for_scope=source.complete_for_scope,
        scope=source.scope,
        jobs=jobs,
        elapsed_seconds=elapsed,
        error=error,
    )


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))

