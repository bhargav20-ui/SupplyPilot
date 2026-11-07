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
4. Enter `GEMINI_API_KEY` and a strong random `APP_ACCESS_KEY` in the secret environment variable fields.
5. Deploy the Blueprint.

## Manual service settings

- Runtime: Python 3.13.5 (pinned by `.python-version` and `PYTHON_VERSION`)
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/api/health`
- Required secret: `GEMINI_API_KEY`
- Required for public use: strong random `APP_ACCESS_KEY` (the dashboard sends it in an `X-App-Key` header)
- Optional: `GEMINI_MODEL=gemini-3.8-flash`

## Verify the live service

- Visit `/api/health`; expect `status: ok` and `ai_configured: true`.
- Open the root page and click **Load sample data**.
- Enter the configured workspace key in the dashboard field, then click **Run agent analysis**.
- A completed investigation should report measured Gemini requests and tool executions.
- A model outage or quota error should appear as a visible failure. Do not interpret a successful page load as proof that Gemini requests work.

Do not share the `GEMINI_API_KEY` or `APP_ACCESS_KEY` with anyone or commit them to Git.