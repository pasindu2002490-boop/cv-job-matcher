"""Capture public career-page network calls to discover structured job feeds.

This is a diagnostic tool: it deliberately stores no request/response headers,
cookies, browser storage, or response bodies.  It records endpoint metadata and
a small structural summary that is sufficient to identify connector candidates.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cv_job_matcher.it_company_sources import (  # noqa: E402
    SRI_LANKA_IT_COMPANY_CAREER_SEEDS,
    VERIFIED_IT_COMPANY_CAREER_URLS,
)


def _json_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        first = value[0] if value else None
        return {
            "root_type": "list",
            "item_count": len(value),
            "first_item_keys": sorted(first)[:40] if isinstance(first, dict) else [],
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {"root_type": "object", "root_keys": sorted(value)[:60]}
        for key, child in value.items():
            if isinstance(child, list):
                first = child[0] if child else None
                result["largest_list"] = {
                    "key": key,
                    "item_count": len(child),
                    "first_item_keys": sorted(first)[:40] if isinstance(first, dict) else [],
                }
                break
        return result
    return {"root_type": type(value).__name__}


def _looks_job_related(url: str, shape: dict[str, Any]) -> bool:
    telemetry_hosts = (
        "google-analytics.com",
        "analytics.google.com",
        "googletagmanager.com",
        "googleadservices.com",
        "doubleclick.net",
        "linkedin.com/attribution",
        "hubspot.com/web-interactives",
    )
    if any(host in url.casefold() for host in telemetry_hosts):
        return False
    terms = ("job", "career", "vacan", "position", "opening", "recruit", "posting")
    shape_text = json.dumps(shape).casefold()
    return any(term in url.casefold() or term in shape_text for term in terms)


def probe(company: str, url: str, page_timeout_ms: int) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-LK",
            timezone_id="Asia/Colombo",
            viewport={"width": 1440, "height": 1000},
        )
        page = context.new_page()

        def capture(response: Response) -> None:
            request = response.request
            content_type = response.headers.get("content-type", "").casefold()
            if request.resource_type not in {"xhr", "fetch", "document"} and "json" not in content_type:
                return
            shape: dict[str, Any] = {}
            if "json" in content_type:
                try:
                    shape = _json_shape(response.json())
                except Exception:
                    shape = {"root_type": "unreadable_json"}
            try:
                post_data = request.post_data if request.method != "GET" else None
            except (UnicodeDecodeError, ValueError):
                post_data = "<binary body omitted>"
            item = {
                "url": response.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "status": response.status,
                "content_type": content_type.split(";", 1)[0],
                "post_data": post_data,
                "json_shape": shape,
            }
            item["job_candidate"] = request.resource_type in {"xhr", "fetch"} and _looks_job_related(
                response.url, shape
            )
            calls.append(item)

        page.on("response", capture)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=page_timeout_ms)
            page.wait_for_timeout(3500)
            for fraction in (0.35, 0.7, 1.0):
                page.evaluate("fraction => window.scrollTo(0, document.body.scrollHeight * fraction)", fraction)
                page.wait_for_timeout(900)
            title = page.title()
            final_url = page.url
            page_status = response.status if response else None
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            title = page.title()
            final_url = page.url
            page_status = None
        finally:
            context.close()
            browser.close()

    unique: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for call in calls:
        key = (call["method"], call["url"], call["post_data"])
        unique[key] = call
    ordered = sorted(unique.values(), key=lambda item: (not item["job_candidate"], item["url"]))
    return {
        "company": company,
        "requested_url": url,
        "final_url": final_url,
        "page_status": page_status,
        "page_title": title,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_count": sum(bool(item["job_candidate"]) for item in ordered),
        "network_calls": ordered,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", help="Comma-separated registry company names; default is all verified companies")
    parser.add_argument("--limit", type=int, default=0, help="Maximum companies to probe; 0 means no limit")
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--out", default="out_career_network_probe.json")
    args = parser.parse_args()

    registry = dict(SRI_LANKA_IT_COMPANY_CAREER_SEEDS)
    requested = [part.strip() for part in args.companies.split(",")] if args.companies else list(VERIFIED_IT_COMPANY_CAREER_URLS)
    unknown = [name for name in requested if name not in registry]
    if unknown:
        parser.error(f"unknown company name(s): {', '.join(unknown)}")
    selected = requested[: args.limit or None]

    results = []
    for index, company in enumerate(selected, 1):
        url = VERIFIED_IT_COMPANY_CAREER_URLS.get(company) or registry[company]
        print(f"[{index}/{len(selected)}] {company}: {url}", flush=True)
        result = probe(company, url, max(args.timeout_seconds, 5) * 1000)
        print(
            f"  status={result['page_status']} candidates={result['candidate_count']} errors={len(result['errors'])}",
            flush=True,
        )
        results.append(result)

    output = Path(args.out).resolve()
    output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved sanitized network report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
