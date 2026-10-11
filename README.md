# JNR Website to PDF — Vercel frontend + Railway backend

This deployment layout keeps the JNR UI on Vercel and runs the Playwright/Chromium backend as a persistent service on Railway. It is an adaptation of the local-only bundle; do not deploy the original package unchanged to Vercel and expect its in-memory job queue to behave as an always-on worker.

## Repository layout

- `vercel-frontend/public/index.html` — static UI deployed to Vercel.
- `vercel-frontend/vercel.json` — proxies `/api` and `/api/*` through Vercel to the backend domain.
- `backend/server.py` — FastAPI API and Chromium renderer.
- `backend/Dockerfile` — installs Python dependencies and the matching Chromium browser.

## 1. Push the project to GitHub

Create a repository and upload all files in this folder while preserving the folder structure.

## 2. Deploy the backend to Railway

1. In Railway, create a new project from your GitHub repository.
2. Configure the service Root Directory as `/backend` (or select the backend directory using Railway's current service settings). The `Dockerfile` will build the service.
3. Deploy. The Dockerfile binds Uvicorn to `0.0.0.0` and Railway's `PORT` value.
4. In Railway Networking, generate a public domain. Open `https://YOUR-BACKEND-DOMAIN/api/health`; it should return JSON with `"ok": true`.

Keep the backend to **one running replica** for this first version. Job metadata is held in process memory and output PDFs are temporary local files; scaling to multiple replicas or restarting during a conversion can make an in-progress job unavailable. A production multi-instance version needs shared job storage/queue (for example Redis) and shared result storage (object storage).

## 3. Connect Vercel to the backend

1. Open `vercel-frontend/vercel.json`. Replace both occurrences of `https://REPLACE-WITH-YOUR-RAILWAY-BACKEND.up.railway.app` with the real Railway HTTPS domain. Do not add a trailing slash.
2. Commit and push the change.
3. In Vercel, import the same GitHub repository as a new project. Set **Root Directory** to `vercel-frontend`. Select **Other**/static as the framework; there is no build step. If the dashboard asks for an Output Directory, set it to `public`. Deploy.

The rewrite forwards the frontend's existing relative `/api/...` requests to Railway. The browser keeps using the Vercel domain, so you do not need to hard-code the backend URL into the HTML or enable cross-origin browser requests. If you later change the backend domain, edit `vercel.json` and redeploy Vercel.

## 4. Verify

- `https://YOUR-VERCEL-PROJECT.vercel.app/` loads the UI.
- `https://YOUR-VERCEL-PROJECT.vercel.app/api/health` returns the backend health JSON (through the Vercel rewrite).
- Submit a small public URL first; confirm status updates, preview and download work.

## Availability and security notes

- Vercel hosts the static UI; Railway runs the actual persistent browser service. A persistent service is the right model for this app's in-memory job lifecycle. This is not a guarantee of zero downtime. Keep an eye on logs, memory usage, concurrency, and service plan limits.
- The API is intended to fetch public websites and has private/reserved-address checks, but an internet-facing crawler can still be abused. Before public launch, add server-side rate limits and an abuse control (authentication or CAPTCHA/Turnstile) and review the browser isolation model. CORS alone is not an API security mechanism.
- Current jobs and rendered PDFs are transient. Deploys/restarts erase job state and temporary output. Do not scale to more than one replica until jobs/results are moved to shared storage.
- It cannot archive content protected by login or bypass website security controls.
