from __future__ import annotations

import argparse
import csv
import html
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
HREF_RE = re.compile(r"""(?is)href\s*=\s*["']([^"'#]+)["']""")
MAILTO_RE = re.compile(r"""(?i)mailto:([^?&#"']+)""")
CF_RE = re.compile(r'data-cfemail=["\']([0-9a-fA-F]+)["\']')
RELEVANT = ("apply", "career", "contact", "job", "vacan", "about", "team", "join", "recruit")
GENERIC = {
    "career", "careers", "cv", "hello", "hr", "info", "job", "jobs",
    "people", "recruit", "recruiting", "recruitment", "talent", "work",
}
SKIP_DOMAINS = {
    "example.com", "domain.com", "email.com", "sentry.io", "wixpress.com",
    "schema.org", "cloudflare.com",
}
AGGREGATOR_HOSTS = (
    "linkedin.com", "topjobs.lk", "itpro.lk", "himalayas.app",
    "weworkremotely.com", "recruit.net", "hire.lk", "drjobpro.com",
    "crossover.com",
)


def fetch(url: str) -> tuple[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CVJobEmailAudit/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=20, context=ssl.create_default_context()) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type and "text" not in content_type:
                return response.geturl(), ""
            return response.geturl(), response.read(1_000_000).decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError, ValueError, ssl.SSLError):
        return url, ""


def decode_cf(value: str) -> str:
    try:
        key = int(value[:2], 16)
        return "".join(chr(int(value[i:i + 2], 16) ^ key) for i in range(2, len(value), 2))
    except (ValueError, IndexError):
        return ""


def clean_email(value: str) -> str:
    return html.unescape(unquote(value)).strip().strip(".,;:()[]{}<>\"'").lower()


def valid_email(email: str) -> bool:
    if not EMAIL_RE.fullmatch(email):
        return False
    domain = email.rsplit("@", 1)[1]
    if domain in SKIP_DOMAINS or any(domain.endswith("." + item) for item in SKIP_DOMAINS):
        return False
    if any(token in email for token in ("yourname", "name@", "user@", "test@", ".png", ".jpg", ".webp")):
        return False
    return True


def emails_in(raw: str) -> set[str]:
    candidates = set(EMAIL_RE.findall(html.unescape(raw)))
    candidates.update(MAILTO_RE.findall(raw))
    candidates.update(decode_cf(value) for value in CF_RE.findall(raw))
    return {email for item in candidates if (email := clean_email(item)) and valid_email(email)}


def relevant_links(base_url: str, raw: str, limit: int = 8) -> list[str]:
    base = urlparse(base_url)
    host = base.netloc.lower().removeprefix("www.")
    if any(host == item or host.endswith("." + item) for item in AGGREGATOR_HOSTS):
        return []
    links: list[str] = []
    seen: set[str] = {base_url.rstrip("/")}
    for href in HREF_RE.findall(raw):
        absolute = urljoin(base_url, html.unescape(href))
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            continue
        normalized = parsed._replace(fragment="", query="").geturl().rstrip("/")
        haystack = f"{parsed.path} {href}".lower()
        if normalized in seen or not any(term in haystack for term in RELEVANT):
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links


def crawl_job(row: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, str]]:
    start = row.get("apply_url", "").strip()
    final_url, raw = fetch(start)
    pages = [(final_url, raw)]
    if raw:
        links = relevant_links(final_url, raw)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch, link): link for link in links}
            for future in as_completed(futures):
                pages.append(future.result())

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_url, page in pages:
        host = urlparse(source_url).netloc.lower()
        # LinkedIn guest pages embed unrelated recommended jobs in their payload,
        # so an address cannot be safely attributed to the viewed vacancy.
        page_emails = set() if host == "linkedin.com" or host.endswith(".linkedin.com") else emails_in(page)
        for email in sorted(page_emails):
            if email in seen:
                continue
            seen.add(email)
            local = email.split("@", 1)[0]
            root = re.split(r"[._+\-]", local, maxsplit=1)[0]
            email_domain = email.rsplit("@", 1)[1]
            page_host = urlparse(source_url).netloc.lower().removeprefix("www.")
            is_platform = (
                any(page_host == item or page_host.endswith("." + item) for item in AGGREGATOR_HOSTS)
                and (email_domain == page_host or page_host.endswith("." + email_domain))
            )
            found.append({
                "company": row.get("company", ""),
                "job_title": row.get("title", ""),
                "email": email,
                "email_type": "generic/company" if local in GENERIC or root in GENERIC else "named/public",
                "email_scope": "job-board/platform contact" if is_platform else "employer/job contact",
                "email_source_url": source_url,
                "apply_url": start,
                "match_score": row.get("match_score", ""),
            })
    status = {
        "company": row.get("company", ""),
        "job_title": row.get("title", ""),
        "apply_url": start,
        "pages_checked": str(sum(bool(page) for _, page in pages)),
        "emails_found": str(len(found)),
        "status": "checked" if raw else "blocked_or_unavailable",
    }
    return found, status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    with args.input_csv.open(encoding="utf-8-sig", newline="") as handle:
        jobs = list(csv.DictReader(handle))

    results: list[dict[str, str]] = []
    audit: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(crawl_job, job) for job in jobs]
        for future in as_completed(futures):
            found, status = future.result()
            results.extend(found)
            audit.append(status)

    results.sort(key=lambda item: (item["company"].lower(), item["email"], item["job_title"].lower()))
    audit.sort(key=lambda item: (item["company"].lower(), item["job_title"].lower()))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fields = ["company", "job_title", "email", "email_type", "email_scope", "email_source_url", "apply_url", "match_score"]
    with (args.output_dir / "job_listing_emails.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    with (args.output_dir / "job_email_crawl_audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(audit[0]) if audit else [])
        if audit:
            writer.writeheader()
            writer.writerows(audit)

    unique = sorted({item["email"] for item in results})
    employer_results = [item for item in results if item["email_scope"] == "employer/job contact"]
    platform_results = [item for item in results if item["email_scope"] == "job-board/platform contact"]
    lines = [
        "# Public emails found in today's matched job listings", "",
        f"- Matched jobs checked: {len(jobs)}",
        f"- Public email occurrences: {len(results)}",
        f"- Unique email addresses: {len(unique)}", "",
        "## Employer/job contact emails", "",
    ]
    for item in employer_results:
        lines.extend([
            f"### {item['company']} — {item['job_title']}", "",
            f"- Email: `{item['email']}`",
            f"- Type: {item['email_type']}",
            f"- Evidence: {item['email_source_url']}", "",
        ])
    lines.extend(["## Job-board/platform contacts (not application emails)", ""])
    for item in platform_results:
        lines.extend([
            f"- `{item['email']}` — {item['company']} / {item['job_title']}",
            f"  Evidence: {item['email_source_url']}",
        ])
    if not results:
        lines.append("No public email addresses were exposed on the retrievable listing or linked company pages.")
    (args.output_dir / "job_listing_emails.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Checked {len(jobs)} jobs; found {len(results)} occurrences / {len(unique)} unique emails.")


if __name__ == "__main__":
    main()
