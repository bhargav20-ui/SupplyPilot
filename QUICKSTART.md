# SupplyPilot quick start

```powershell
cd path\to\SupplyPilot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Set `GEMINI_API_KEY` in `.env`, save it, then run:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, click **Load sample data**, then **Run agent analysis**. An actual investigation makes six Gemini model requests: four parallel specialists, one dependent procurement planner and one dependent independent auditor. The dashboard reports measured API/tool counts. Without a working Gemini key/model, the app must fail visibly rather than display fabricated findings.