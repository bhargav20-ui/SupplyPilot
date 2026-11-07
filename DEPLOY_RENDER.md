# Render deployment checklist

## Before deployment

- [ ] Create an empty GitHub repository named `SupplyPilot` (do not initialize it with a README, license, or `.gitignore`).
- [ ] Push the project with `PUBLISH_TO_GITHUB.ps1` or your normal Git workflow.
- [ ] Confirm `.env` is ignored and no API key is in the repository.
- [ ] Confirm `render.yaml` is present at the repository root.

## Blueprint deployment

1. Open the Render Dashboard and select **New → Blueprint**.
2. Connect the GitHub repository containing this project.
3. Review the service named `supplypilot`.
4. Enter `GEMINI_API_KEY` in the secret environment variable field. Visitors use public guest access and do not need a shared key.
5. Deploy the Blueprint.

## Manual service settings

- Runtime: Python 3.13.5 (pinned by `.python-version` and `PYTHON_VERSION`)
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/api/health`
- Required secret: `GEMINI_API_KEY`
- Public guest rate limit: 5 runs per visitor IP per 15 minutes by default
- Maximum simultaneous investigations per process: 2 by default
- Optional: `GEMINI_MODEL=gemini-3.8-flash`

## Verify the live service

- Visit `/api/health`; expect `status: ok` and `ai_configured: true`.
- Open the root page and click **Load sample data**.
- Click **Run analysis**. No workspace key is required.
- `POST /api/investigate` should return HTTP 202 with a task ID quickly. The dashboard then polls `GET /api/investigations/{task_id}`; long Gemini workflows no longer depend on a single open HTTP request.
- A completed investigation should report measured Gemini requests and tool executions.
- A model outage or quota error should appear as a visible failure. Do not interpret a successful page load as proof that Gemini requests work.

Never expose `GEMINI_API_KEY` to the browser or commit it to Git. The guest rate limiter is process-local and intended for a demo; active investigation tasks are lost on process restart/redeploy. Use a durable queue and persistent task store plus Redis-backed quotas, user accounts and abuse monitoring before scaling to a large public audience.

## Gemini quota and public traffic

- One complete SupplyPilot investigation normally makes six Gemini generation requests: four specialists, one planner, and one auditor. A malformed output retry may add one or more requests.
- Gemini free-tier quotas are shared across all visitors using this project's API key and apply at the Google Cloud project level. The free tier is not suitable for unlimited public traffic.
- A daily quota error is not retried. SupplyPilot opens a process-local circuit breaker for the provider's `RetryInfo` duration and exposes the pause through `/api/health`.
- For wider public access, link billing in Google AI Studio, set a conservative spend cap, monitor Usage, and implement persistent cross-instance quotas and abuse controls.


## Gemini daily quota protection

- One completed investigation normally consumes six Gemini generation requests; repair retries may add requests. All anonymous visitors share the same server-side Gemini project quota.
- A daily `RESOURCE_EXHAUSTED` response is not retried. SupplyPilot reads Google's `RetryInfo`, fails the current job clearly, and pauses new jobs for the indicated interval.
- `/api/health` exposes `gemini_quota_paused` and `gemini_quota_retry_after_seconds`; the dashboard shows the temporary pause.
- The current quota circuit breaker is in memory and applies to this app process only. A service restart clears it; it is not persistent across instances.
- For public traffic beyond a small demo, enable an appropriate Gemini paid tier, configure a conservative budget/spending alert, inspect Usage and Rate limits, and move rate-limit/quota state to a persistent shared store.

After deploying, inspect `/api/health` and confirm `ai_configured: true`. If the daily quota has already been exhausted, wait for Google to restore quota or update the project's billing/tier; repeatedly clicking Run will not fix it.