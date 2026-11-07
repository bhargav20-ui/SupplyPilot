# SupplyPilot — Multi-Agent Supply Chain Decision Intelligence

SupplyPilot is a decision-support application that coordinates six Gemini-backed agents to turn sales history, inventory position, and supplier performance into an evidence-backed replenishment proposal.

It is deliberately **not** an inventory CRUD dashboard. Each specialist has a bounded analytical responsibility, the procurement planner depends on the specialists' findings, and an independent auditor challenges the resulting proposal. The app never places purchase orders or contacts suppliers.

## What it does

- Forecasts SKU-level demand from recent sales history using a damped 7-day trend calculation.
- Measures days of inventory cover, reorder points, safety-stock exposure, and target stock.
- Scores suppliers per SKU using delivery reliability (47%), quality (38%), and relative unit-cost competitiveness (15%).
- Assesses single-source exposure, recorded capacity, and resilience gaps.
- Builds MOQ- and capacity-constrained procurement options with estimated cost.
- Uses a final Gemini planning agent to synthesize the specialist findings and an independent Gemini auditor to challenge the recommendation.
- Reports actual Gemini request counts and deterministic analytics-tool executions. A provider failure is surfaced; no mock AI findings are substituted.

## Multi-agent architecture

```text
Sales history ──> Demand Forecasting Agent ───────┐
Inventory data ─> Inventory Risk Agent ────────────┤
Supplier data ──> Supplier Intelligence Agent ────┼─> Procurement Planning Agent
Inventory + suppliers -> Supply Risk Agent ───────┘          │
              deterministic constraint checks ──────────────┤
                                                            v
                                                Independent Decision Auditor
                                                            │
                                                            v
                                             Human-reviewed proposal + evidence
```

The four specialists run concurrently. Each specialist receives output from its own analytical tool and a narrow role prompt. The planner receives all four findings plus the constraint-checked order options. The auditor receives the planner output, all specialist findings, and the numeric constraints. These are actual separate model requests, not static sample outputs.

### Analytics tools used by the agents

| Tool | Purpose |
|---|---|
| `forecast_demand` | Per-SKU recent and prior run rates with damped trend |
| `calculate_inventory_exposure` | Days of cover, reorder point, target stock, risk classification |
| `rank_suppliers` | Weighted supplier score, lead time, MOQ, capacity |
| `assess_supply_risk` | Supplier count, concentration, basic resilience checks |
| `build_procurement_plan` | Reorder quantity allocation within supplier MOQ/capacity constraints |
| `validate_procurement_plan` | Deterministic hard checks before human review |

The current version uses local Python analytics functions rather than MCP. MCP is intentionally not added just to tick a box; it can be introduced later when the app connects to external inventory, ERP, or supplier systems.

## Input data

All three datasets are required. Use the **Load sample data** button or upload CSVs.

### `sales_history.csv`

Required columns: `date`, `sku`, `product`, `units_sold`

### `inventory.csv`

Required columns: `sku`, `product`, `on_hand`, `lead_time_days`, `safety_stock`

### `suppliers.csv`

Required columns: `supplier`, `sku`, `unit_cost`, `lead_time_days`, `on_time_rate`, `quality_rate`, `capacity_units`, `min_order_qty`. Optional: `currency` (defaults to `USD`; keep every supplier row in the same currency).

Supplier rates may be given as decimals (0.96) or percentages (96). The sample data uses illustrative USD figures. Set the optional `currency` column to your actual currency (for example, `INR`) and keep all supplier rows in one currency; SupplyPilot blocks mixed-currency supplier files rather than comparing unlike amounts.

## Run locally (Windows PowerShell)

```powershell
# 1. Open the project directory
cd path\to\SupplyPilot

# 2. Create and activate a virtual environment
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
python -m pip install -r requirements.txt

# 4. Create your local environment file
Copy-Item .env.example .env
notepad .env
```

Add your Gemini API key to `.env` (and optionally a local access key):

```env
GEMINI_API_KEY=your_real_key
GEMINI_MODEL=gemini-3.8-flash
# Optional: require this key before allowing investigation runs
APP_ACCESS_KEY=
```

Then start the application:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

The `/api/health` endpoint confirms whether the API key is configured. Loading sample data works without a key, but an actual agent investigation requires a valid Gemini API key and model access. The server returns a visible error if Gemini is unavailable.

## Run tests

```powershell
python -m pip install pytest
python -m pytest -q
```

The tests cover the deterministic forecasting, risk scoring, supplier ranking, and procurement constraints. They do not make paid Gemini API requests.

## Deploy to Render

1. Push this folder to a GitHub repository.
2. In Render, choose **New → Blueprint** and select the repository containing `render.yaml`.
3. Add both `GEMINI_API_KEY` and a strong, random `APP_ACCESS_KEY` as secret environment variables when prompted. The workspace key protects the public investigation endpoint from anonymous use of your Gemini quota.
4. Keep `GEMINI_MODEL` set to `gemini-3.8-flash` unless your Gemini account requires another available model.
5. Deploy. Render uses the included health check at `/api/health`.

You can also create a Render Web Service manually with:

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Environment variables: `GEMINI_API_KEY` (secret), `APP_ACCESS_KEY` (strong secret), `GEMINI_MODEL=gemini-3.8-flash`

## Important limitations and safety

- Synthetic sample data is illustrative and must not be presented as operational company data.
- Forecasts use a simple, explainable recent-run-rate heuristic, not a trained demand-forecasting model.
- Supplier scores are configurable decision-support weights, not objective certifications.
- Costs are estimates based on the uploaded supplier file; the app does not model taxes, freight, rebates, duties, currency conversion, or contract terms.
- Replenishment quantities are constrained by supplied MOQs and supplier capacities, but a human must verify current stock, purchase agreements, and delivery terms.
- No ERP, live supplier, payment, or purchasing integration is included in this MVP.
- Never upload credentials, personal data, or confidential commercial records without authorization and appropriate controls. Configure `APP_ACCESS_KEY` for a public deployment; without it, anyone who can reach the service may be able to trigger model usage.

## Environment variables

| Name | Required | Default | Purpose |
|---|---:|---|---|
| `GEMINI_API_KEY` | Yes for AI runs | — | Gemini Developer API key |
| `GEMINI_MODEL` | No | `gemini-3.8-flash` | Gemini model identifier |
| `APP_ACCESS_KEY` | Recommended for public deployment | — | Shared key required to start an investigation; entered in the dashboard and sent only in the request header |

## Project structure

```text
SupplyPilot/
├── app/
│   ├── agents.py          # Six-agent Gemini orchestration
│   ├── core.py            # Deterministic analytics and constraint checks
│   └── main.py            # FastAPI endpoints and static app
├── data/sample/           # Clearly labelled synthetic CSV datasets
├── static/                # Responsive dashboard UI
├── tests/                 # Unit tests for analytics and constraints
├── .env.example
├── render.yaml
└── requirements.txt
```

## Publish to GitHub from Windows PowerShell

Create a **new empty GitHub repository** named `SupplyPilot` first (do not initialize it with a README, license, or `.gitignore`). In PowerShell, from the extracted project folder, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\PUBLISH_TO_GITHUB.ps1 -RepositoryUrl "https://github.com/YOUR_USERNAME/SupplyPilot.git"
```

The script initializes the local Git repository, commits the project, sets `origin`, and pushes `main`. It never stages `.env` because `.gitignore` excludes it.