# Netlify frontend

This directory contains only the browser interface. It submits work to the
separate API and never crawls or matches jobs inside Netlify.

## Local build

1. Install Node.js 20 or newer.
2. Copy `.env.example` to `.env`.
3. Set the public API URL, Supabase project URL, and Supabase publishable key.
4. Run:

   ```sh
   npm install
   npm run build
   ```

The deployable site is generated in `dist/`. For a quick local preview, run
`npm run dev`.

## Netlify settings

- Base directory: `frontend`
- Build command: `npm run build`
- Publish directory: `dist`
- Node version: 20 or newer

Configure these public build variables in Netlify:

- `VITE_API_BASE_URL` — the HTTPS Cloud Run API origin, without a trailing slash.
- `VITE_SUPABASE_URL` — the Supabase project URL.
- `VITE_SUPABASE_PUBLISHABLE_KEY` — the browser-safe publishable key.
- `VITE_ALLOW_ANONYMOUS=false` — keep authentication required in production.
- `VITE_POLL_INTERVAL_MS=2500` — optional polling interval.
- `VITE_MAX_CV_BYTES=10485760` — optional client-side upload limit; the API must
  enforce its own limit too.

Do not put the Supabase service-role key, database password, Groq key, Resend
key, or Google credentials in a `VITE_` variable. Build-time browser values are
visible to every site visitor.

`VITE_SUPABASE_ANON_KEY` is accepted as a temporary fallback for older Supabase
projects, but new deployments should use the publishable-key variable above.

## API contract used by the interface

Authenticated requests include `Authorization: Bearer <Supabase access token>`.
Submission also includes a stable `Idempotency-Key` header:

- `POST /v1/tasks` as multipart form data with `cv`, `country`, `position`,
  `years_experience`, and `remote`.
- `GET /v1/tasks/:id` for persistent status.
- `GET /v1/tasks/:id/results` for fresh private download links.

The status page tolerates either a direct task object or `{ "task": ... }`, and
common result shapes such as `result_files`, `files`, or `results.files`.
Download URLs should be short-lived signed URLs returned only to the task owner.
