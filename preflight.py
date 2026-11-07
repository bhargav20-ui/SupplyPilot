from pathlib import Path
import csv
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
required = [
    "app/main.py", "app/core.py", "app/agents.py", "static/index.html",
    "static/styles.css", "static/app.js", "requirements.txt", "render.yaml",
    "README.md", "DEPLOY_RENDER.md", "ARCHITECTURE.md",
]
missing = [name for name in required if not (root / name).is_file()]
if missing:
    raise SystemExit("Missing project files: " + ", ".join(missing))
for name in ("sales_history.csv", "inventory.csv", "suppliers.csv"):
    path = root / "data" / "sample" / name
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"Empty sample dataset: {name}")
    print(f"OK sample dataset: {name} ({len(rows)} rows)")
node = shutil.which("node")
if node:
    subprocess.run([node, "--check", str(root / "static" / "app.js")], check=True)
    print("OK frontend JavaScript syntax")
else:
    print("SKIP Node.js syntax check (Node.js not installed in build environment)")
print("OK required project files present")
print("Run automated tests with: python -m pytest -q")
print("Gemini investigation requires google-genai and GEMINI_API_KEY in the runtime environment.")