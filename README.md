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
- `job_matches.csv` - ranked live job listings with apply links, source attribution, and contact columns when enabled
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
- ITPro.lk AI/Data RSS feed for Sri Lanka
- topjobs.lk public vacancy pages for Sri Lanka
- RemoteRocketship public job pages for Sri Lanka remote roles
- LinkedIn public guest job search pages for Sri Lanka, where available
- XpressJobs public JSON search for Sri Lanka
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
position, and experience years. A hard experience gate first removes roles above the
candidate's entered experience. Groq then strictly checks every remaining job for
experience and target-field fit before the CSV reports are emailed.

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
```

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
$env:CRAWL4AI_SEED_URLS="https://example.com/careers,https://example.com/jobs"
```

## Output interpretation

`match_score` is a ranking score, not a legal eligibility guarantee. Visa status, work authorization, language requirements, certifications, and employer-specific constraints must still be verified manually before applying.

By default, country searches prefer country-local sources. Use `--include-remote-global` to include worldwide remote boards.
