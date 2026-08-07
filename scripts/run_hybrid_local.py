from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cv_job_matcher.agent_graph import AgentGraphOptions, VerticalJobAgentGraph
from cv_job_matcher.country import normalize_country
from cv_job_matcher.cv_parser import parse_cv, read_cv
from cv_job_matcher.llm_filter import FileReviewCheckpointStore
from cv_job_matcher.shared_matcher import SharedInventoryMatchOptions, run_shared_inventory_match


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Use the CV and requested role to crawl every applicable site, then run hybrid matching."
    )
    parser.add_argument("--cv", required=True, type=Path)
    parser.add_argument("--position", required=True)
    parser.add_argument("--experience-years", required=True, type=float)
    parser.add_argument("--country", default="Sri Lanka")
    parser.add_argument("--out", type=Path, default=Path("out_hybrid"))
    parser.add_argument("--include-remote-global", action="store_true")
    parser.add_argument("--limit-per-site", type=int, default=80)
    args = parser.parse_args()

    profile = replace(
        parse_cv(read_cv(args.cv)),
        target_position=args.position.strip(),
        experience_years=args.experience_years,
    )
    graph = VerticalJobAgentGraph(
        AgentGraphOptions(
            limit_per_source=args.limit_per_site,
            include_remote_global=args.include_remote_global,
            web_discovery=True,
        )
    )
    print(
        f"Attempting {len(graph.providers)} configured source agents for "
        f"{args.position!r} in {args.country}..."
    )
    discovery = graph.run(profile, normalize_country(args.country))
    jobs = discovery.jobs
    productive = sum(
        trace.status == "completed_with_results" for trace in discovery.traces
    )
    failed = sum(trace.status == "failed" for trace in discovery.traces)
    skipped = sum(trace.status == "skipped" for trace in discovery.traces)
    empty = sum(
        trace.status in {
            "connector_empty_unverified",
            "completed_inventory_no_role_candidates",
        }
        for trace in discovery.traces
    )
    print(
        "Role-specific discovery complete: "
        f"{productive} returned candidates, {empty} returned no role candidates, "
        f"{skipped} skipped, {failed} failed, {len(jobs)} relevant rows collected."
    )

    args.out.mkdir(parents=True, exist_ok=True)
    summary = run_shared_inventory_match(
        SharedInventoryMatchOptions(
            cv_path=args.cv,
            jobs=jobs,
            country=args.country,
            position=args.position,
            experience_years=args.experience_years,
            out_dir=args.out,
            include_remote_global=args.include_remote_global,
            source_traces=discovery.traces,
        ),
        checkpoint_store=FileReviewCheckpointStore(args.out / "llm_review_checkpoint.jsonl"),
    )
    print(f"Related jobs: {summary.related_jobs}")
    print(f"Final matches: {summary.matches_written}")
    print(f"Results: {summary.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
