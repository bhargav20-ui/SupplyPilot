const $ = (id) => document.getElementById(id);
const datasetFields = ["sales", "inventory", "suppliers"];
let lastResult = null;

function parseCSV(text) {
  const rows = []; let row = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '"' && quoted && text[i + 1] === '"') { cell += '"'; i++; }
    else if (ch === '"') quoted = !quoted;
    else if (ch === ',' && !quoted) { row.push(cell); cell = ""; }
    else if ((ch === '\n' || ch === '\r') && !quoted) {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); if (row.some(v => v.trim() !== "")) rows.push(row);
      row = []; cell = "";
    } else cell += ch;
  }
  row.push(cell); if (row.some(v => v.trim() !== "")) rows.push(row);
  if (rows.length < 2) throw new Error("CSV must include a header row and at least one data row.");
  const headers = rows.shift().map(v => v.trim().replace(/^\uFEFF/, ""));
  return rows.map(cells => Object.fromEntries(headers.map((header, i) => [header, normalizeCell(cells[i] ?? "")] )));
}
function normalizeCell(value) {
  const trimmed = String(value).trim();
  if (trimmed === "") return "";
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  return trimmed;
}
function setDataset(name, rows) {
  $(name + "-input").value = rows.length ? JSON.stringify(rows, null, 2) : "";
  $(name + "-count").textContent = rows.length ? `${rows.length.toLocaleString()} rows loaded` : "No data loaded";
}
function getDataset(name) {
  const raw = $(name + "-input").value.trim();
  if (!raw) throw new Error(`${name[0].toUpperCase()+name.slice(1)} data is required.`);
  if (raw.startsWith("[")) {
    const data = JSON.parse(raw);
    if (!Array.isArray(data)) throw new Error(`${name} JSON must be an array of objects.`);
    return data;
  }
  return parseCSV(raw);
}
function setAgentState(name, state) {
  const card = document.querySelector(`[data-agent="${CSS.escape(name)}"]`);
  if (!card) return;
  card.classList.remove("active", "completed", "failed");
  if (state === "running") card.classList.add("active");
  if (state === "completed") card.classList.add("completed");
  if (state === "failed") card.classList.add("failed");
  const label = card.querySelector(".agent-state");
  if (label) label.textContent = state.toUpperCase();
}
function setAllAgents(state) {
  document.querySelectorAll("[data-agent]").forEach(card => {
    card.classList.remove("active", "completed", "failed");
    const label = card.querySelector(".agent-state"); if (label) label.textContent = state.toUpperCase();
  });
}
function showError(message) {
  $("error-banner").classList.remove("hidden"); $("error-message").textContent = message;
}
function clearError() { $("error-banner").classList.add("hidden"); $("error-message").textContent = ""; }
function currency(value, code="USD") {
  try { return new Intl.NumberFormat(undefined, {style:"currency",currency:code,maximumFractionDigits:0}).format(value); }
  catch { return `${code} ${Number(value).toLocaleString()}`; }
}
function renderResult(result) {
  lastResult = result;
  const data = result.data, plan = data.procurement_plan;
  const risks = data.inventory_exposure.filter(x => ["CRITICAL","HIGH","MEDIUM"].includes(x.risk_level)).length;
  $("stat-skus").textContent = data.inventory_exposure.length;
  $("stat-risk").textContent = risks;
  $("stat-units").textContent = plan.total_units.toLocaleString();
  $("stat-cost").textContent = currency(plan.estimated_cost, plan.currency);
  $("risk-badge").textContent = `OVERALL RISK · ${result.overall_risk}`;
  $("risk-badge").className = `risk-badge ${result.overall_risk.toLowerCase()}`;
  $("risk-badge").classList.remove("hidden");
  $("execution-badge").className = "execution-badge done";
  $("execution-badge").innerHTML = "<i></i> INVESTIGATION COMPLETE";
  for (const name of Object.keys(result.agents)) setAgentState(name, "completed");
  setAgentState("Procurement Planning Agent", "completed");
  setAgentState("Decision Auditor Agent", "completed");
  $("decision-summary").textContent = result.planner.decision_summary || "Procurement options generated.";
  const plannerActions = Array.isArray(result.planner.priority_actions) ? result.planner.priority_actions : [];
  const rationale = Array.isArray(result.planner.procurement_rationale) ? result.planner.procurement_rationale : [];
  const actions = [...plannerActions, ...rationale.slice(0,2)];
  $("priority-actions").innerHTML = actions.length ? actions.map(x => `<div class="output-action">${escapeHTML(x)}</div>`).join("") : `<div class="empty-state">No priority actions returned.</div>`;
  const audit = result.auditor;
  $("audit-status").innerHTML = `<span class="audit-icon">${result.status === "ready_for_human_review" ? "✓" : "!"}</span><strong>${result.status === "ready_for_human_review" ? "Ready for human review" : "Human review required"}</strong>`;
  $("audit-copy").textContent = (audit.concerns || []).join(" ") || (result.status === "ready_for_human_review" ? "The auditor did not identify a blocking concern in the submitted evidence." : "The auditor did not approve this plan for human review. Read the checks and required actions below.");
  const auditChecks = Array.isArray(audit.checks) ? audit.checks : [];
  const hardIssues = Array.isArray(data.hard_constraint_issues) ? data.hard_constraint_issues : [];
  const checks = [...auditChecks.map(x => ({label:x.check, result:x.result})), ...(hardIssues.map(x=>({label:x,result:"fail"})))];
  $("audit-checks").innerHTML = checks.slice(0,5).map(x=>`<div class="audit-check ${escapeHTML(x.result)}"><b>${x.result === "pass" ? "✓" : x.result === "warning" ? "!" : "×"}</b><span>${escapeHTML(x.label)}</span></div>`).join("");
  $("orders-body").innerHTML = plan.orders.length ? plan.orders.map(o=>`<tr><td class="sku-cell"><strong>${escapeHTML(o.sku)}</strong><small>${escapeHTML(o.product)}</small></td><td>${escapeHTML(o.supplier)}</td><td>${o.quantity.toLocaleString()}</td><td>${currency(o.unit_cost, plan.currency)}</td><td class="cost-cell">${currency(o.estimated_cost,plan.currency)}</td><td>${o.lead_time_days} days</td><td><span class="score-chip">${o.supplier_score}/100</span></td></tr>`).join("") : `<tr><td colspan="7" class="table-empty">No reorder quantity was generated for the submitted stock and demand.</td></tr>`;
  $("order-count").textContent = `${plan.total_order_lines} ORDER LINES`;
  $("table-bottom").textContent = `${plan.total_units.toLocaleString()} proposed units · Estimated total ${currency(plan.estimated_cost,plan.currency)} · ${plan.note}`;
  if (plan.shortages.length) {
    $("shortage-panel").classList.remove("hidden");
    $("shortage-list").innerHTML = plan.shortages.map(x=>`<div class="shortage-item"><b>${escapeHTML(x.sku)}</b> · ${x.uncovered_units} units uncovered — ${escapeHTML(x.reason)}</div>`).join("");
  } else $("shortage-panel").classList.add("hidden");
  const insightNames = ["Demand Forecasting Agent","Inventory Risk Agent","Supplier Intelligence Agent","Supply Risk Agent"];
  $("agent-insights").classList.remove("hidden");
  $("agent-insights").innerHTML = insightNames.map(name=>{
    const a = result.agents[name] || {};
    const keyFindings = Array.isArray(a.key_findings) ? a.key_findings : [];
    const risks = Array.isArray(a.risks) ? a.risks : [];
    const items = [...keyFindings, ...risks.slice(0,2)];
    return `<article class="insight-card"><h4>${escapeHTML(name)}</h4><ul>${items.slice(0,5).map(x=>`<li>${escapeHTML(x)}</li>`).join("") || "<li>No findings returned.</li>"}</ul></article>`;
  }).join("");
  $("sample-note").textContent = `${result.metrics.gemini_calls} Gemini calls · ${result.metrics.tool_calls} analytics tool calls · ${result.model}`;
}
function escapeHTML(value) { return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch])); }

async function loadHealth() {
  try {
    const response = await fetch("/api/health"); const health = await response.json();
    $("runtime-label").textContent = health.ai_configured ? "AI runtime configured" : "API key required";
    $("model-label").textContent = `${health.model} · Gemini API${health.access_key_required ? " · access key enabled" : ""}`;
  } catch {
    $("runtime-label").textContent = "Runtime unavailable";
    $("model-label").textContent = "Check server connection";
  }
}
async function loadSample() {
  clearError();
  try {
    const response = await fetch("/api/sample"); if (!response.ok) throw new Error("Could not load sample datasets.");
    const data = await response.json(); datasetFields.forEach(name => setDataset(name, data[name]));
    $("sample-note").textContent = data.label + " · sample only";
  } catch (error) { showError(error.message); }
}
async function runAnalysis() {
  clearError();
  let payload;
  try {
    payload = {
      title: $("investigation-title").value.trim(),
      sales: getDataset("sales"), inventory: getDataset("inventory"), suppliers: getDataset("suppliers")
    };
    if (payload.title.length < 5) throw new Error("Please enter an investigation brief of at least five characters.");
  } catch (error) { showError(error.message); return; }
  $("run-analysis").disabled = true; $("run-analysis").innerHTML = "Investigating… <span>◌</span>";
  $("execution-badge").className = "execution-badge running"; $("execution-badge").innerHTML = "<i></i> AGENTS WORKING";
  setAllAgents("queued");
  ["Demand Forecasting Agent","Inventory Risk Agent","Supplier Intelligence Agent","Supply Risk Agent"].forEach(name=>setAgentState(name,"running"));
  $("decision-summary").textContent = "Specialists are analyzing your evidence. The planner will wait for their findings.";
  $("audit-status").innerHTML = `<span class="audit-icon">◌</span><strong>Waiting for specialist findings</strong>`;
  $("audit-copy").textContent = "No approval decision is available until the independent audit completes.";
  try {
    const headers = {"Content-Type":"application/json"};
    const workspaceKey = $("workspace-key").value.trim();
    if (workspaceKey) headers["X-App-Key"] = workspaceKey;
    const response = await fetch("/api/investigate", {method:"POST",headers,body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) {
      const partial = result.metrics ? ` Partial execution: ${result.metrics.gemini_calls} Gemini request(s), ${result.metrics.tool_calls} analytics tool(s). No completed investigation was published.` : "";
      throw new Error((result.detail || "The investigation failed.") + partial);
    }
    renderResult(result);
  } catch (error) {
    $("execution-badge").className = "execution-badge"; $("execution-badge").innerHTML = "<i></i> RUN FAILED";
    setAllAgents("not completed");
    showError(error.message);
  } finally {
    $("run-analysis").disabled = false; $("run-analysis").innerHTML = "Run agent analysis <span>↗</span>";
  }
}

$("load-sample").addEventListener("click", loadSample);
$("clear-data").addEventListener("click", ()=>{datasetFields.forEach(name=>setDataset(name,[]));lastResult=null;$("sample-note").textContent="Paste JSON or import CSV datasets";});
$("run-analysis").addEventListener("click", runAnalysis);
$("dismiss-error").addEventListener("click", clearError);
for (const name of datasetFields) {
  $(name+"-file").addEventListener("change", async event=>{
    const file = event.target.files?.[0]; if (!file) return;
    try { const rows = parseCSV(await file.text()); setDataset(name, rows); }
    catch (error) { showError(`${file.name}: ${error.message}`); }
    event.target.value = "";
  });
  $(name+"-input").addEventListener("input",()=>{
    const text=$(name+"-input").value.trim(); const count=text?text.split(/\r?\n/).length-1:0;
    $(name+"-count").textContent=count>0?`${count} lines · parse on run`:"No data loaded";
  });
}
loadHealth();