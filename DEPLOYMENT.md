# Deployment Steps

This repository should be deployed as separate services, not as one combined Render app.

## Target architecture

- Netlify hosts only the frontend.
- Cloud Run hosts the API.
- Cloud Run Jobs host the crawler and matching worker.
- Supabase provides PostgreSQL and private file storage.

## Deployment order

1. Provision Supabase.
2. Deploy the API to Cloud Run.
3. Deploy the crawler as a scheduled Cloud Run Job.
4. Deploy the matching worker as a Cloud Run Job.
5. Deploy the frontend to Netlify.
6. Configure secrets, scheduler, and smoke tests.

## Step-by-step

### 1) Set up Supabase

- Create a Supabase project.
- Use PostgreSQL for persistent state.
- Create private storage buckets for uploaded CVs and generated result files.
- Keep the service-role key server-side only.

Required persistent data includes:

- tasks
- jobs
- source_runs
- job_reviews
- result_files

### 2) Deploy the API service

Build and deploy [docker/Dockerfile.api](docker/Dockerfile.api) as a Cloud Run service.

The API is implemented in [src/cv_job_matcher/cloud_api.py](src/cv_job_matcher/cloud_api.py).

Responsibilities:

- authenticate the user
- accept CV uploads
- create a durable task record
- store the CV privately
- return task status and result links
- optionally trigger the matcher job

### 3) Deploy the crawler job

Build and deploy [docker/Dockerfile.crawler](docker/Dockerfile.crawler) as a Cloud Run Job.

The crawler logic is in [src/cv_job_matcher/cloud_worker.py](src/cv_job_matcher/cloud_worker.py) with `crawl` mode.

Responsibilities:

- refresh the shared job inventory
- write source run records
- upsert discovered jobs into PostgreSQL

Schedule this job with Cloud Scheduler every 5 to 10 minutes.

### 4) Deploy the matching worker

Build and deploy [docker/Dockerfile.matcher](docker/Dockerfile.matcher) as a Cloud Run Job.

The worker logic is in [src/cv_job_matcher/cloud_worker.py](src/cv_job_matcher/cloud_worker.py) with `match` mode.

Responsibilities:

- claim the next queued task
- load the private CV from storage
- match against the shared inventory
- checkpoint each LLM decision immediately
- write result files
- email the user

### 5) Deploy the frontend

Deploy the [frontend](frontend) folder to Netlify.

Netlify settings:

- Base directory: `frontend`
- Build command: `npm run build`
- Publish directory: `dist`
- Node version: 20 or newer

Public environment variables:

- `VITE_API_BASE_URL`
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_PUBLISHABLE_KEY`
- `VITE_ALLOW_ANONYMOUS=false`
- `VITE_POLL_INTERVAL_MS=2500`
- `VITE_MAX_CV_BYTES=10485760`

### 6) Configure secrets

Keep the following out of the repository and out of Netlify public variables:

- `SUPABASE_SERVICE_ROLE_KEY`
- `DATABASE_URL`
- `GROQ_API_KEY`
- `RESEND_API_KEY`
- `SMTP_PASSWORD`

Store them in Secret Manager or the equivalent secret store used by the platform.

### 7) Configure the build pipeline

[deploy/cloudbuild.yaml](deploy/cloudbuild.yaml) already builds the three container images.

Use it to:

- build the API image
- build the crawler image
- build the matcher image
- push them to Artifact Registry
- deploy the matching Cloud Run service and jobs

## Validation before deploy

Run these checks before pushing to production:

1. `pytest -q`
2. `Set-Location frontend; npm run build`
3. Smoke test the API health endpoint

Expected result:

- tests pass
- frontend bundle is generated in `frontend/dist`
- API health returns HTTP 200

## Notes

- Do not use [render.yaml](render.yaml) as the production deployment blueprint.
- The frontend should never crawl or match jobs itself.
- The crawler should work from a shared inventory, not per-user scraping.