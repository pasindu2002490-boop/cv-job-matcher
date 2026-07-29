# CV Job Matcher

Evidence-backed CV and live job matcher.

This tool takes:

- a CV file (`.txt`, `.md`, `.pdf`, or `.docx`)
- a target country
- a target position
- the candidate's experience years

It outputs:

- `tailored_cv.md` - an ATS-friendly CV draft aligned to the strongest matching roles
- `all_discovered_jobs.csv` - every opening retrieved from configured sources before ranking filters
- `related_vacancies.csv` - every live, country-compatible vacancy selected by broad target-role keywords before LLM review
- `job_matches.csv` - ranked live job listings with apply links, source attribution, and contact columns when enabled
- `rejected_vacancies.csv` - every final-stage rejection with its decision reason
- `manual_review_vacancies.csv` - vacancies whose local model response could not be validated; these are never mislabeled as rejections
- `source_coverage.csv` - per-source status plus discovered, related, accepted, and rejected counts
- `job_matches.md` - human-readable report with source coverage, timestamps, and match reasons
- `companies_hiring.csv` / `companies_hiring.md` - application tracker style list with contact columns when enabled
- `company_contacts.csv` / `company_contacts.md` - optional public recruiting/contact leads per matched company
- `source_audit.md` - source coverage and pending site integrations

## Important accuracy note

No system can honestly guarantee "all jobs in a country" or "100% accurate" results because job ads change constantly, some sites block scraping, and most large job boards require private API contracts. This project is designed for high-integrity output instead:

- every listing includes a source and URL
- every run includes a fetch timestamp
- results are deduplicated
- matches show why the candidate appears eligible
- optional paid/broad APIs can be enabled with credentials
- contact enrichment records public evidence and never guesses emails from naming patterns

## Live job sources

Built in:

- Remotive public API for active remote jobs
- Himalayas public remote jobs API
- RemoteOK public remote jobs API
- We Work Remotely public RSS feeds
- ITPro.lk dynamic category inventory and pagination for Sri Lanka
- topjobs.lk complete open-vacancy inventory and pagination for Sri Lanka
- RemoteRocketship public job pages for Sri Lanka remote roles
- LinkedIn public guest job search pages for Sri Lanka, where available
- XpressJobs complete active JSON inventory with record-count pagination for Sri Lanka
- dedicated bounded portal crawlers for CareerLK, Hire.lk, Recruiter.lk,
  LankaJob.lk, Inseeks, Observer Jobs, JobPal, Ikman Jobs, CareerFirst,
  CSE Careers, Government Jobs, Jobber.lk, JobFactory.lk, DreamJobs.lk,
  JobEka.lk, FindMyJob.lk, Career141, TimesJobs.lk, GovernmentJobs.lk,
  GovernmentVacancies.lk, Gazette.lk, job.govdoc.lk, SLBFE Job Bank,
  LankaQualityJobs.com, Recruitme.lk, Jobup.lk, and MYJOBS.LK
- DuckDuckGo search discovery with `--web-discovery`
- Google Custom Search with `GOOGLE_CSE_API_KEY` or `GOOGLE_API_KEY` plus `GOOGLE_CSE_ID`
- SerpAPI Google with `SERPAPI_API_KEY`
- Crawl4AI seed hook with `CRAWL4AI_ENABLED=1` and `CRAWL4AI_SEED_URLS`
- Arbeitnow public API for Germany and Europe-oriented jobs
- Adzuna API when `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` are configured

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## Web form with automatic email delivery

The web application lets a user upload a CV, enter an email address, target country,
position, and experience years. The root agent extracts the CV once, then all enabled
source agents run concurrently. Source connectors build or query their inventories
without an LLM, results are merged and deduplicated, and Groq performs one final
strict review of every related vacancy against the extracted CV evidence before the
CSV reports are emailed. If Groq returns a rate-limit, quota, transport, provider, or
invalid-JSON failure, that run immediately switches the failed batch and all remaining
vacancies to local Ollama; decisions already completed by Groq are preserved. Ollama
reviews one vacancy per request with exact response-ID validation. Configurable
head-and-tail evidence limits protect the LLM context window and record when unusually
long text was truncated.

The current graph contains 42 configured source agents (local, optional search, and
opt-in global sources). `GET /health` reports the configured count plus Crawl4AI,
Groq, local Ollama fallback, and SMTP readiness without exposing credentials.

Install the project and configure SMTP settings. For Gmail, use an app password rather
than the normal account password:

```powershell
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USERNAME="your-account@gmail.com"
$env:SMTP_PASSWORD="your-app-password"
$env:SMTP_FROM="your-account@gmail.com"
$env:SMTP_USE_TLS="1"
$env:GROQ_API_KEY="your-groq-api-key"
$env:GROQ_MODEL="openai/gpt-oss-20b"
$env:OLLAMA_FALLBACK_ENABLED="1"
$env:OLLAMA_BASE_URL="http://127.0.0.1:11434"
$env:OLLAMA_MODEL="llama3.1:8b"
$env:OLLAMA_CONTEXT_SIZE="8192"
$env:OLLAMA_KEEP_ALIVE="30m"
$env:OLLAMA_BATCH_SIZE="2"
$env:OLLAMA_NUM_PREDICT="512"
$env:OLLAMA_CV_CHAR_LIMIT="3000"
$env:OLLAMA_JOB_DESCRIPTION_CHAR_LIMIT="1800"
$env:LLM_CV_CHAR_LIMIT="16000"
$env:LLM_JOB_DESCRIPTION_CHAR_LIMIT="2500"
$env:SOURCE_AGENT_WORKERS="8"
$env:SOURCE_RESULT_LIMIT="5000"
$env:SOURCE_INVENTORY_CACHE_MINUTES="30"
$env:WEB_DISCOVERY_MAX_QUERIES_PER_SOURCE="4"
$env:WEB_DISCOVERY_MAX_DETAIL_PAGES_PER_SOURCE="30"
$env:WEB_DISCOVERY_DETAIL_WORKERS="6"
```

Install Ollama separately and pull the fallback model once with
`ollama pull llama3.1:8b`. The default local request timeout is 300 seconds to
accommodate a cold model load. Local review uses compact structured CV evidence
and exact two-vacancy batches. If a two-row response fails exact ID validation,
both vacancies are retried individually. Every completed decision is appended to
`llm_review_checkpoint.jsonl`; a programmatic or CLI retry that reuses the same
candidate and output directory resumes unfinished rows. Browser submissions use a
new task directory, so they do not currently expose this resume mechanism. A
malformed singleton is written to
`manual_review_vacancies.csv` while the rest of the run continues.

Set `OLLAMA_FALLBACK_ENABLED=0` only when Groq-only
retry behavior is specifically required. Final and rejected CSV rows record the
provider and model that reviewed each vacancy.

Start the web application:

```powershell
.\.venv\Scripts\python.exe -m cv_job_matcher.web
```

On Windows, the secure launcher is easier and avoids storing the App Password in a file:

```powershell
.\start_web.ps1 -Email "your-account@gmail.com"
```

It securely prompts for both the Gmail App Password and Groq API key, then starts the
server with both configured in the same process. Keep that PowerShell window open
while searches are running. Create a Groq key at `https://console.groq.com/keys`.

Then open `http://127.0.0.1:8000`. Uploaded CV files are deleted after each run.
Generated reports remain under `web_data/results/<task-id>` by default. Configure
`UPLOAD_ROOT`, `OUTPUT_ROOT`, `MAX_CV_UPLOAD_MB`, `WEB_HOST`, `WEB_PORT`, and
`WEB_WORKERS` as needed.

For production, run behind HTTPS and use a durable task queue/database instead of the
in-memory status store if jobs must survive server restarts.

`SOURCE_RESULT_LIMIT` is the maximum relevant rows returned by one connector to a web
run. The high web default avoids silently cutting a large role at 200 rows.
`SOURCE_INVENTORY_CACHE_MINUTES` reuses complete ITPro, TopJobs, and XpressJobs
inventories across positions, while each request still performs its own local
role/CV filtering. Set the cache to a small value when near-real-time refresh matters.
The bounded `HTTP_GET_CACHE_*` settings reuse successful anonymous source pages for
five minutes and coalesce identical concurrent downloads, including searches from
different users. `DISCOVERY_RESULT_CACHE_*` additionally reuses an exact repeated
profile/query fan-out for ten minutes; its key includes every discovery input, so
different CV evidence cannot receive another candidate's role-filtered result set.
Search-engine snippets are never treated as vacancies by themselves. DuckDuckGo,
Google CSE, and SerpAPI fetch and validate a bounded set of result detail pages
concurrently. The three `WEB_DISCOVERY_*` settings cap queries, fetched detail pages,
and detail workers per search provider; internal hard ceilings still protect public
endpoints and paid API quotas from accidental unbounded runs.

## Publish with GitHub and Render

GitHub stores the source code and Render runs the Python web service. GitHub Pages is
not suitable because this application requires server-side Python and background work.

1. Push this repository to GitHub. Candidate PDFs, output folders, `.env`, SMTP
   passwords, and local virtual environments are excluded by `.gitignore`.
2. Sign in to Render and choose **New > Blueprint**.
3. Connect the GitHub repository and select its `render.yaml` Blueprint.
4. Enter secret values when Render requests them:
   `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `GROQ_API_KEY`. Optionally
   configure the search-provider keys declared in `render.yaml`.
5. Deploy. Render assigns an HTTPS `onrender.com` URL and redeploys after pushes to
   the connected GitHub branch.

The included production command uses one Gunicorn process with multiple threads so
the current in-memory task status remains consistent. The generated reports live on
Render's ephemeral filesystem only long enough to be emailed. For higher traffic or
jobs that must survive deploys/restarts, migrate background execution to Celery plus
Render Key Value and store results in durable object storage.

Run:

```powershell
python -m cv_job_matcher --cv .\sample_cv.txt --country "Germany" --position "Backend Engineer" --experience-years 5 --out .\out
```

Sri Lanka local-only example:

```powershell
python -m cv_job_matcher --cv ".\AI ML Engineer - B R G Lakmal (1).pdf" --country srilanka --position "AI Engineer" --experience-years 2 --out .\out_lakmal_srilanka_local
```

Include worldwide remote boards too:

```powershell
python -m cv_job_matcher --cv ".\AI ML Engineer - B R G Lakmal (1).pdf" --country srilanka --position "AI Engineer" --experience-years 2 --include-remote-global --out .\out_lakmal_srilanka_remote
```

Discovery mode with search-engine expansion:

```powershell
python -m cv_job_matcher --cv ".\AI ML Engineer - B R G Lakmal (1).pdf" --country srilanka --position "AI Engineer" --experience-years 2 --include-remote-global --web-discovery --minimum-score 0 --out .\out_lakmal_srilanka_world
```

Add public company contact enrichment:

```powershell
python -m cv_job_matcher --cv ".\AI ML Engineer - B R G Lakmal (1).pdf" --country srilanka --position "AI Engineer" --experience-years 2 --include-remote-global --web-discovery --find-contacts --contact-limit-companies 50 --out .\out_lakmal_srilanka_contacts
```

The contact flow creates LinkedIn people-search links for HR/recruiting contacts and tries to find public recruiting emails from search/page evidence. It does not scrape private LinkedIn pages, bypass login walls, or guess personal email patterns. Personal emails are excluded by default; use `--include-public-personal-emails` only for emails explicitly present in public evidence.

Optional LLM filtering with OpenAI:

```powershell
$env:OPENAI_API_KEY="your_key"
python -m cv_job_matcher --cv ".\AI ML Engineer - B R G Lakmal (1).pdf" --country srilanka --position "AI Engineer" --experience-years 2 --include-remote-global --web-discovery --llm-filter --llm-model gpt-4.1-mini --out .\out_lakmal_srilanka_llm
```

Optional LLM filtering with Groq:

```powershell
$env:GROQ_API_KEY="your_groq_key"
$env:GROQ_MODEL="openai/gpt-oss-20b"
python -m cv_job_matcher --cv ".\AI ML Engineer - B R G Lakmal (1).pdf" --country srilanka --position "AI Engineer" --experience-years 2 --include-remote-global --web-discovery --llm-filter --llm-model $env:GROQ_MODEL --out .\out_lakmal_srilanka_groq
```

Optional Adzuna credentials:

```powershell
$env:ADZUNA_APP_ID="your_app_id"
$env:ADZUNA_APP_KEY="your_app_key"
```

Optional Google/SerpAPI discovery:

```powershell
$env:GOOGLE_CSE_API_KEY="your_google_key"
$env:GOOGLE_CSE_ID="c4a4f5750d7f04ebc"
$env:SERPAPI_API_KEY="your_serpapi_key"
```

Optional Crawl4AI seed discovery:

```powershell
$env:CRAWL4AI_ENABLED="1"
$env:CRAWL4AI_SEED_URLS="https://careerlk.com/jobs/,https://www.hire.lk/jobs,https://www.recruiter.lk/jobs"
```

## Output interpretation

`match_score` is a ranking score, not a legal eligibility guarantee. Visa status, work authorization, language requirements, certifications, and employer-specific constraints must still be verified manually before applying.

By default, country searches prefer country-local sources. Use `--include-remote-global` to include worldwide remote boards.

Read `source_coverage.csv` before interpreting a zero. `completed_with_results` means
the connector returned validated role candidates.
`completed_inventory_no_role_candidates` means a complete current inventory was
loaded but the broad keyword gate selected no rows. `connector_empty_unverified`
means only that the connector returned no rows; it does **not** prove that the
website had no matching vacancies. `skipped` and `failed` explain unavailable
credentials, disabled options, blocks, timeouts, or parser failures.
