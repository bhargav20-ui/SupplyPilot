const $ = (id) => document.getElementById(id);
const datasetFields = ["sales", "inventory", "suppliers"];
let lastResult = null;

function wait(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    throw new Error("Could not reach SupplyPilot. Check your connection and try again.");
  }

  const raw = await response.text();
  let data;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    const status = response.status ? `HTTP ${response.status}` : "unknown HTTP status";
    if (response.status === 502 || response.status === 503 || response.status === 504) {
      throw new Error(`SupplyPilot's hosting service returned ${status}. The service may be starting, restarting or temporarily unavailable. Wait a moment and retry. If it keeps happening, check the Render service logs.`);
    }
    throw new Error(`SupplyPilot returned a webpage instead of API data (${status}). This usually indicates a hosting or deployment problem. Please retry; if it persists, check Render logs.`);
  }

  if (!response.ok) {
    const retry = response.headers.get("Retry-After");
    let message = data.detail || data.error || `Request failed (${response.status}).`;
    if (response.status === 429 && retry) message += ` You can retry in about ${retry} seconds.`;
    throw new Error(message);
  }
  return data;
}

function updateTaskProgress(task) {
  const agents = task.agents || {};
  for (const [name, status] of Object.entries(agents)) setAgentState(name, status);
  const done = Object.values(agents).filter(status => status === "completed").length;
  const running = Object.values(agents).filter(status => status === "running").length;
  const state = task.status === "queued" ? "QUEUED" : task.status === "running" ? `AGENTS ${done}/6 COMPLETE${running ? ` · ${running} RUNNING` : ""}` : task.status.toUpperCase();
  $("execution-badge").className = `execution-badge ${task.status === "running" ? "running" : ""}`;
  $("execution-badge").innerHTML = `<i></i> ${escapeHTML(state)}`;
}

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
  const count = $(name + "-count");
  count.textContent = rows.length ? `${rows.length.toLocaleString()} rows loaded` : "Required";
  count.classList.toggle("loaded", rows.length > 0);
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
  $("audit-status").className = `audit-status ${result.status === "ready_for_human_review" ? "approved" : "review"}`;
  $("audit-status").innerHTML = `<span class="audit-icon">${result.status === "ready_for_human_review" ? "✓" : "!"}</span><strong>${result.status === "ready_for_human_review" ? "Ready for human review" : "Human review required"}</strong>`;
  $("audit-copy").textContent = (audit.concerns || []).join(" ") || (result.status === "ready_for_human_review" ? "The auditor did not identify a blocking concern in the submitted evidence." : "The auditor did not approve this plan for human review. Read the checks and required actions below.");
  const auditChecks = Array.isArray(audit.checks) ? audit.checks : [];
  const hardIssues = Array.isArray(data.hard_constraint_issues) ? data.hard_constraint_issues : [];
  const checks = [...auditChecks.map(x => ({label:x.check, result:x.result})), ...(hardIssues.map(x=>({label:x,result:"fail"})))];
  $("audit-checks").innerHTML = checks.slice(0,5).map(x=>`<div class="audit-check ${escapeHTML(x.result)}"><b>${x.result === "pass" ? "✓" : x.result === "warning" ? "!" : "×"}</b><span>${escapeHTML(x.label)}</span></div>`).join("");
  $("orders-body").innerHTML = plan.orders.length ? plan.orders.map(o=>`<tr><td class="sku-cell"><strong>${escapeHTML(o.sku)}</strong><small>${escapeHTML(o.product)}</small></td><td>${escapeHTML(o.supplier)}</td><td>${o.quantity.toLocaleString()}</td><td class="cost-cell">${currency(o.estimated_cost,plan.currency)}</td><td>${o.lead_time_days} days</td></tr>`).join("") : `<tr><td colspan="5" class="table-empty">No reorder quantity was generated for the submitted stock and demand.</td></tr>`;
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
    const health = await requestJson("/api/health");
    $("runtime-label").textContent = health.ai_configured ? "AI ready · Public access" : "Gemini API key required";
    $("model-label").textContent = health.model;
  } catch {
    $("runtime-label").textContent = "Runtime unavailable";
    $("model-label").textContent = "Check connection";
  }
}

async function loadSample() {
  clearError();
  try {
    const data = await requestJson("/api/sample");
    for (const name of datasetFields) {
      if (!Array.isArray(data[name]) || data[name].length === 0) throw new Error(`Sample ${name} data is missing or empty. Please contact the administrator.`);
      setDataset(name, data[name]);
    }
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

  $("run-analysis").disabled = true;
  $("run-analysis").textContent = "Starting analysis…";
  $("execution-badge").className = "execution-badge running";
  $("execution-badge").innerHTML = "<i></i> QUEUING INVESTIGATION";
  setAllAgents("queued");
  $("decision-summary").textContent = "Your investigation has been queued. Specialists will begin shortly.";
  $("audit-status").innerHTML = `<span class="audit-icon">◌</span><strong>Waiting for specialist findings</strong>`;
  $("audit-copy").textContent = "The independent auditor will review the plan after the specialists and planner finish.";

  try {
    const started = await requestJson("/api/investigate", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    if (!started.task_id) throw new Error("The server did not return an investigation task ID. Please retry. If this repeats, check the Render logs.");

    let transientPollFailures = 0;
    while (true) {
      let task;
      try {
        task = await requestJson(`/api/investigations/${encodeURIComponent(started.task_id)}`);
        transientPollFailures = 0;
      } catch (error) {
        transientPollFailures += 1;
        if (transientPollFailures >= 5) throw error;
        $("execution-badge").innerHTML = `<i></i> RECONNECTING (${transientPollFailures}/5)`;
        await wait(1800);
        continue;
      }

      updateTaskProgress(task);
      if (task.status === "completed") {
        if (!task.result || !task.result.data || !task.result.planner || !task.result.auditor) {
          throw new Error("The server marked this task complete but returned an incomplete result. No procurement recommendation was displayed. Please retry and report this run ID: " + started.task_id);
        }
        renderResult(task.result);
        break;
      }
      if (task.status === "failed") {
        $("execution-badge").className = "execution-badge";
        $("execution-badge").innerHTML = "<i></i> RUN FAILED";
        const counts = task.metrics || {};
        const partial = ` Partial execution: ${counts.gemini_calls || 0} Gemini request(s), ${counts.tool_calls || 0} analytics tool(s).`;
        showError((task.error || "The investigation could not complete.") + partial + ` Run ID: ${started.task_id}`);
        break;
      }
      await wait(1500);
    }
  } catch (error) {
    $("execution-badge").className = "execution-badge";
    $("execution-badge").innerHTML = "<i></i> RUN FAILED";
    setAllAgents("not completed");
    showError(error.message);
  } finally {
    $("run-analysis").disabled = false;
    $("run-analysis").textContent = "Run analysis";
  }
}

$("load-sample").addEventListener("click", loadSample);
$("clear-data").addEventListener("click", ()=>{
  datasetFields.forEach(name=>setDataset(name,[]));
  lastResult=null;
  $("sample-note").textContent="Use the sample data to try SupplyPilot, or upload your own CSV files.";
  $("agent-insights").classList.add("hidden");
  $("risk-badge").classList.add("hidden");
  $("execution-badge").className="execution-badge";
  $("execution-badge").innerHTML="<i></i> Waiting for data";
  setAllAgents("idle");
  $("decision-summary").textContent="Your procurement recommendation will appear here after analysis.";
  $("priority-actions").innerHTML="";
  $("audit-status").className="audit-status";
  $("audit-status").innerHTML=`<span class="audit-icon">—</span><strong>Not started</strong>`;
  $("audit-copy").textContent="The auditor will review the proposed plan after the specialist agents finish.";
  $("audit-checks").innerHTML="";
  $("orders-body").innerHTML=`<tr><td colspan="5" class="table-empty">Run an analysis to generate procurement options.</td></tr>`;
  $("order-count").textContent="No analysis yet";
  $("table-bottom").textContent="Planning estimates only. No purchase orders are placed by SupplyPilot.";
  $("shortage-panel").classList.add("hidden");
  clearError();
});
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
    const countLabel = $(name+"-count");
    countLabel.textContent=count>0?`${count} lines entered`:"Required";
    countLabel.classList.toggle("loaded",count>0);
  });
}
loadHealth();