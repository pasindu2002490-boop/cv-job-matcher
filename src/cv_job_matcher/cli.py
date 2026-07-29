from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from .runner import RunOptions, run_match


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="Create a tailored CV and live job match report.")
    parser.add_argument("--cv", required=True, type=Path, help="Path to CV file (.txt, .md, .pdf, .docx).")
    parser.add_argument("--country", required=True, help="Target country, e.g. Germany, United States, UK.")
    parser.add_argument("--position", default="", help="Target position, e.g. AI Engineer, ML Engineer, Data Scientist.")
    parser.add_argument("--experience-years", type=float, default=None, help="Candidate experience years.")
    parser.add_argument(
        "--include-remote-global",
        action="store_true",
        help="Include worldwide/global remote job boards in addition to country-local sources.",
    )
    parser.add_argument(
        "--web-discovery",
        action="store_true",
        help="Use search-engine discovery across known sites in addition to direct providers.",
    )
    parser.add_argument(
        "--llm-filter",
        action="store_true",
        help="Use an OpenAI-compatible LLM to filter/rerank after retrieval when OPENAI_API_KEY or GROQ_API_KEY is configured.",
    )
    parser.add_argument("--llm-model", default="gpt-4.1-mini", help="LLM model name for --llm-filter.")
    parser.add_argument("--llm-limit", type=int, default=80, help="Maximum ranked jobs to send to the LLM filter.")
    parser.add_argument("--out", type=Path, default=Path("out"), help="Output directory.")
    parser.add_argument("--limit-per-source", type=int, default=200, help="Max jobs to request per source.")
    parser.add_argument("--minimum-score", type=float, default=40.0, help="Minimum match score to include.")
    parser.add_argument(
        "--find-contacts",
        action="store_true",
        help="Create company_contacts.csv/md with public recruiting emails and LinkedIn HR search leads.",
    )
    parser.add_argument(
        "--contact-limit-companies",
        type=int,
        default=50,
        help="Maximum matched companies to enrich when --find-contacts is enabled.",
    )
    parser.add_argument(
        "--contact-results-per-query",
        type=int,
        default=3,
        help="Maximum Google CSE results per contact-discovery query.",
    )
    parser.add_argument(
        "--include-public-personal-emails",
        action="store_true",
        help="Include personal emails only when they are explicitly present in public search/page evidence.",
    )
    args = parser.parse_args(argv)

    summary = run_match(RunOptions(
        cv_path=args.cv,
        country=args.country,
        position=args.position,
        experience_years=args.experience_years,
        out_dir=args.out,
        include_remote_global=args.include_remote_global,
        web_discovery=args.web_discovery,
        llm_filter=args.llm_filter,
        llm_model=args.llm_model,
        llm_limit=args.llm_limit,
        limit_per_source=args.limit_per_source,
        minimum_score=args.minimum_score,
        find_contacts=args.find_contacts,
        contact_limit_companies=args.contact_limit_companies,
        contact_results_per_query=args.contact_results_per_query,
        include_public_personal_emails=args.include_public_personal_emails,
    ))

    print(f"Read CV: {args.cv}")
    print(f"Country: {summary.country}")
    print(f"Jobs fetched: {summary.jobs_fetched}")
    print(f"Matches written: {summary.matches_written}")
    if args.find_contacts:
        print(f"Contact leads written: {summary.contact_leads_written}")
    print(f"Output: {summary.output_dir}")
    return 0
