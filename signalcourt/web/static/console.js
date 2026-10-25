"use strict";
const $ = (id) => document.getElementById(id);
const stages = [...document.querySelectorAll("[data-stage]")].map((el) => el.dataset.stage);
const idleFeed = $("feed-empty").cloneNode(true);
const state = {targets: [], selected: null, mode: "live", ready: false, busy: false, id: null, result: null,
  source: null, start: 0, clock: null, currentStage: null, stageStart: 0, lastEvent: 0, pending: false, missingPolls: 0};
const seconds = (n) => Number(n || 0).toFixed(1);

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    const message = typeof detail === "string" ? detail : detail?.message || (detail?.status === "ambiguous" ? "Choose a company from the candidates." : "The request could not be completed.");
    const error = new Error(message); error.detail = detail; error.status = response.status; throw error;
  }
  return response;
}
function notify(message, error = false) { $("notice").textContent = message; $("notice").classList.toggle("error", error); $("notice").hidden = !message; }
function updateRunButton() { $("run-button").disabled = !state.ready || state.busy || state.pending || !state.selected; $("run-button").firstChild.textContent = state.busy ? "Run in progress " : state.pending ? "Starting… " : "Run research "; }
function companyMode(company) {
  return company.run_mode || state.targets.find((target) => target.symbol === company.symbol)?.run_mode || "live";
}
function updateRunHelp() {
  $("run-help").textContent = !state.selected ? "Select a company to begin." : state.mode === "replay" ? "Replay for this demonstration company. No network calls." : "Fetch public filings and run the research.";
}
function option(company) {
  const button = document.createElement("button"); button.type = "button"; button.className = "company-option";
  button.setAttribute("aria-pressed", String(state.selected?.symbol === company.symbol));
  const name = document.createElement("strong"); name.textContent = company.name;
  const meta = document.createElement("span"); meta.className = "company-meta";
  const symbol = document.createElement("span"); symbol.textContent = company.symbol;
  const subtype = document.createElement("span"); subtype.className = "subtype"; subtype.textContent = (company.subtype || "listed").replaceAll("_", " ");
  meta.append(symbol, subtype); button.append(name, meta);
  button.addEventListener("click", () => selectCompany(company)); return button;
}
function renderCompanies() {
  const query = $("company-search").value.trim().toLowerCase();
  const filtered = state.targets.filter((c) => `${c.name} ${c.symbol} ${c.subtype}`.toLowerCase().includes(query));
  $("company-list").replaceChildren(...filtered.map(option));
  if (query) {
    const resolve = document.createElement("button"); resolve.className = "resolve-button"; resolve.textContent = `Resolve “${$("company-search").value.trim()}” ↗`;
    resolve.addEventListener("click", resolveSearch); $("company-list").append(resolve);
  } else if (!filtered.length) { const text = document.createElement("p"); text.className = "no-results"; text.textContent = "No companies found. Search by full name or symbol."; $("company-list").append(text); }
}
function selectCompany(company) {
  if (state.busy || state.pending) return;
  if (state.id) resetIdle();
  state.selected = company; state.mode = companyMode(company); renderCompanies(); updateRunHelp();
  $("run-title").textContent = company.name;
  $("run-subtitle").textContent = `${company.symbol} / ${(company.subtype || "listed company").replaceAll("_", " ")} · Ready to investigate`;
  notify(""); updateRunButton();
}
async function resolveSearch() {
  const query = $("company-search").value.trim(); if (!query || state.busy) return;
  try {
    const data = await (await api(`/api/resolve?q=${encodeURIComponent(query)}`)).json();
    if (data.status === "match") { selectCompany(data.match); $("search-help").textContent = `Resolved: ${data.match.name}`; }
    else if (data.status === "ambiguous") showCandidates(data.candidates);
    else notify(data.message || "No company matched. Try a full name or symbol.", true);
  } catch (error) { notify(error.message, true); }
}
function showCandidates(candidates) { $("company-list").replaceChildren(...candidates.map(option)); $("search-help").textContent = "Several companies match. Choose the one named by the judges."; state.selected = null; updateRunHelp(); updateRunButton(); }
function resetStages() {
  document.querySelectorAll("[data-stage]").forEach((el) => {
    el.className = "stage pending"; el.querySelector(".stage-state").textContent = "Pending";
    el.querySelector(".stage-time").replaceChildren(document.createTextNode("—"));
    el.querySelector(".stage-records").textContent = "0 records"; el.querySelector(".stage-impl").textContent = "—";
  });
}
function resetIdle() {
  resetStages(); state.id = null; state.result = null;
  $("court-feed").replaceChildren(idleFeed.cloneNode(true)); $("failure").hidden = true; $("show-report").hidden = true;
  $("run-clock").textContent = "000.0"; $("run-budget").value = 0; $("budget-label").textContent = "180 s presentation budget";
  $("run-clock").closest(".clock-block").classList.remove("over");
  $("feed-state").textContent = "Awaiting a run"; $("feed-state").classList.remove("active"); $("run-reference").textContent = "No run selected";
}
function startClock(elapsed = 0) {
  clearInterval(state.clock); state.start = performance.now() - elapsed * 1000;
  const tick = () => {
    const elapsed = (performance.now() - state.start) / 1000;
    $("run-clock").textContent = seconds(elapsed).padStart(5, "0"); $("run-budget").value = elapsed;
    $("run-clock").closest(".clock-block").classList.toggle("over", elapsed > 180);
    $("budget-label").textContent = elapsed > 180 ? "Past 180 s · research continues" : "180 s presentation budget";
    if (state.currentStage) {
      const tile = document.querySelector(`[data-stage="${state.currentStage}"] .stage-time`);
      if (tile) tile.textContent = `${seconds((performance.now() - state.stageStart) / 1000)} s`;
    }
  };
  tick(); state.clock = setInterval(tick, 100);
}
function addNote(note, forcedRole) {
  $("feed-empty")?.remove();
  const feed = $("court-feed"); const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 100;
  const lower = note.toLowerCase(); const role = forcedRole || (lower.includes("red team") ? "redteam" : lower.includes("investigator") ? "investigator" : lower.includes("advocate") ? "advocate" : "system");
  const entry = document.createElement("div"); entry.className = `feed-entry ${role}`;
  const time = document.createElement("time"); time.textContent = `${seconds((performance.now() - state.start) / 1000)}s`;
  const who = document.createElement("span"); who.className = "feed-role"; who.textContent = role === "redteam" ? "Red team" : role === "system" ? "Pipeline" : role;
  const text = document.createElement("p"); text.textContent = note; entry.append(time, who, text); feed.append(entry);
  if (stick) feed.scrollTop = feed.scrollHeight;
}
function showLive() { document.body.classList.remove("results"); $("live-view").hidden = false; $("result-view").hidden = true; }
function prepareRun(company, mode) {
  showLive(); resetStages(); $("court-feed").replaceChildren(); $("failure").hidden = true; $("show-report").hidden = true;
  $("run-title").textContent = company.name; $("run-subtitle").textContent = `${company.symbol || ""} / ${mode === "replay" ? "CACHED REPLAY" : "PUBLIC FILINGS RESEARCH"}`;
  $("feed-state").textContent = "Research in progress"; $("feed-state").classList.add("active"); state.lastEvent = 0;
  state.currentStage = null; state.missingPolls = 0; state.busy = true; updateRunButton(); startClock();
}
async function runResearch() {
  if (state.busy || state.pending || !state.ready) return;
  if (!state.selected) { await resolveSearch(); if (!state.selected) return; }
  const company = {...state.selected};
  state.pending = true; updateRunButton(); notify("");
  try {
    const data = await (await api("/api/runs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({company: company.name, symbol: company.symbol})})).json();
    state.mode = data.mode; updateRunHelp(); prepareRun(company, data.mode); state.id = data.id; $("run-reference").textContent = data.id;
    sessionStorage.removeItem("signal-court-report"); connectEvents(data.id);
  } catch (error) {
    if (error.detail?.candidates?.length) showCandidates(error.detail.candidates);
    notify(error.message, true);
    if (error.status === 409) await pollStatus();
  } finally { state.pending = false; updateRunButton(); }
}
function connectEvents(id, after = 0) {
  state.source?.close(); const source = new EventSource(`/api/runs/${encodeURIComponent(id)}/events?after=${after}`); state.source = source;
  ["stage_start", "stage_end", "stage_error", "stage_note", "stop", "error", "done"].forEach((kind) => source.addEventListener(kind, (event) => {
    if (!event.data) return;
    let data; try { data = JSON.parse(event.data); } catch { return; }
    const number = Number(event.lastEventId); if (number && number <= state.lastEvent) return;
    state.lastEvent = number; handleEvent(kind, data);
  }));
  source.onopen = () => { if (state.busy) $("feed-state").textContent = "Research in progress"; };
  source.onerror = (event) => { if (!event.data && state.busy) { $("feed-state").textContent = "Reconnecting to live events…"; notify("Live connection interrupted. The run continues; reconnecting automatically."); } };
}
function handleEvent(kind, data) {
  if (kind.startsWith("stage_") && data.stage) {
    const tile = document.querySelector(`[data-stage="${data.stage}"]`); if (!tile) return;
    if (kind === "stage_start") {
      tile.className = "stage running"; tile.querySelector(".stage-state").textContent = "Running";
      tile.querySelector(".stage-impl").textContent = data.impl; state.currentStage = data.stage; state.stageStart = data.elapsed_seconds === undefined ? performance.now() : state.start + data.elapsed_seconds * 1000;
      addNote(`${data.stage === "judge" ? "Checks" : data.stage.charAt(0).toUpperCase() + data.stage.slice(1)} started.`, "system");
    } else if (kind === "stage_end") {
      tile.className = "stage done"; tile.querySelector(".stage-state").textContent = "Done";
      tile.querySelector(".stage-time").textContent = `${seconds(data.seconds)} s`;
      tile.querySelector(".stage-records").textContent = `${data.records} records`; tile.querySelector(".stage-impl").textContent = data.impl; state.currentStage = null;
    } else if (kind === "stage_note") addNote(data.note);
    else { tile.className = "stage failed"; tile.querySelector(".stage-state").textContent = "Failed"; }
  } else if (kind === "stop") addNote(`STOP / ${data.reason}${data.open_items?.length ? ` · ${data.open_items.join(", ")}` : ""}`, "system");
  else if (kind === "done" || kind === "error") {
    state.source?.close(); state.source = null; state.busy = false; state.currentStage = null; clearInterval(state.clock); updateRunButton();
    $("run-clock").textContent = seconds(data.total_seconds).padStart(5, "0"); $("run-budget").value = data.total_seconds;
    $("feed-state").classList.remove("active"); notify("");
    if (kind === "done") {
      $("feed-state").textContent = "Research complete"; addNote(`Report ready in ${seconds(data.total_seconds)} seconds.`, "system");
      $("show-report").hidden = false;
      const completedId = state.id, title = state.selected?.name || $("run-title").textContent;
      showResult(completedId, title); loadCompanies();
    } else {
      $("feed-state").textContent = "Run stopped"; $("failure").hidden = false;
      $("failure-title").textContent = `Stopped at ${data.stage || "setup"}`; $("failure-reason").textContent = data.error;
      $("retry-button").disabled = false;
      const tile = document.querySelector(`[data-stage="${data.stage}"]`); if (tile) { tile.className = "stage failed"; tile.querySelector(".stage-state").textContent = "Failed"; }
      addNote(data.error, "system");
    }
  }
}
function showResult(id, company) {
  state.result = id; $("result-title").textContent = company; $("verification").hidden = true;
  $("verify-button").textContent = "Verify ledger"; $("verify-button").disabled = false;
  const url = `/runs/${encodeURIComponent(id)}/report`; $("open-report").href = url; $("report-frame").src = url;
  $("live-view").hidden = true; $("result-view").hidden = false; document.body.classList.add("results");
  sessionStorage.setItem("signal-court-report", JSON.stringify({id, company})); $("new-run").focus();
}
async function verifyLedger() {
  const id = state.result; $("verify-button").disabled = true; $("verify-button").textContent = "Verifying…";
  try {
    const result = await (await api(`/api/runs/${encodeURIComponent(id)}/verify`, {method: "POST"})).json();
    if (state.result !== id) return;
    const el = $("verification"); el.hidden = false; el.classList.toggle("fail", !result.ok);
    el.textContent = `${result.status} · ${result.records} records · head ${result.head_hash}${result.ok ? "" : ` · ${result.problems.map((p) => `seq ${p.seq}: ${p.kind}`).join(", ")}`}`;
    $("verify-button").textContent = result.ok ? "✓ Ledger OK" : "Ledger FAIL";
  } catch (error) { notify(error.message, true); $("verify-button").textContent = "Verify ledger"; }
  finally { $("verify-button").disabled = false; }
}
async function downloadPdf() {
  $("pdf-button").disabled = true; $("pdf-button").textContent = "Exporting PDF…";
  try {
    const response = await api(`/api/runs/${encodeURIComponent(state.result)}/pdf`, {method: "POST"});
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const a = document.createElement("a");
    a.href = url; a.download = `signal-court-${state.result}.pdf`; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
    notify("PDF downloaded.");
  } catch (error) { notify(error.message, true); }
  finally { $("pdf-button").disabled = false; $("pdf-button").textContent = "Download PDF"; }
}
async function showHistory() {
  const drawer = $("history-drawer"); if (!drawer.open) drawer.showModal(); $("history-list").textContent = "Loading saved runs…";
  try {
    const runs = await (await api("/api/runs")).json(); const list = $("history-list"); list.replaceChildren();
    if (!runs.length) list.textContent = "No saved runs yet. Completed research will appear here.";
    runs.forEach((run) => {
      const row = document.createElement("button"); row.className = "history-row";
      const name = document.createElement("strong"); name.textContent = run.company;
      const meta = document.createElement("span"); meta.className = "history-meta";
      const status = document.createElement("span"); status.textContent = run.has_report ? "Complete" : "Incomplete";
      const mode = document.createElement("span"); mode.textContent = `${run.mode === "replay" ? "REPLAY" : "RESEARCH"} / ${seconds(run.total_seconds)} s`;
      const date = document.createElement("span"); date.textContent = new Date(run.time).toLocaleString(); meta.append(status, mode, date);
      const ref = document.createElement("small"); ref.textContent = `${run.id}${run.has_report ? "" : " · No finished report"}`;
      row.append(name, meta, ref); row.disabled = !run.has_report;
      row.addEventListener("click", () => { drawer.close(); showResult(run.id, run.company); notify(""); }); list.append(row);
    });
  } catch (error) { $("history-list").textContent = error.message; }
}
async function loadCompanies() {
  try { state.targets = await (await api("/api/companies")).json(); $("target-count").textContent = state.targets.length; renderCompanies(); }
  catch (error) { notify(error.message, true); }
}
async function pollStatus() {
  try {
    const data = await (await api("/api/status")).json(); state.ready = data.state === "ready";
    const status = $("server-status"); status.className = `server-status ${data.state === "ready" ? "" : data.state}`;
    status.replaceChildren(Object.assign(document.createElement("i"), {ariaHidden: "true"}), document.createTextNode(data.state === "ready" ? "Ready" : data.state === "error" ? "Startup failed" : "Starting…"));
    if (data.state === "error") notify(`Startup failed: ${data.message}. Restart the launcher after checking the local workspace.`, true);
    if (data.active_run && data.active_run.id !== state.id && !state.pending) {
      const run = data.active_run; state.busy = false; state.selected = {name: run.company, symbol: run.symbol}; state.mode = run.mode; updateRunHelp();
      renderCompanies();
      prepareRun(state.selected, run.mode); state.id = run.id; $("run-reference").textContent = run.id; startClock(run.elapsed_seconds);
      run.events.forEach((e) => { state.lastEvent = e.id; handleEvent(e.event, e.data); }); connectEvents(run.id, state.lastEvent);
      notify("Reconnected to the run already in progress.");
    }
    if (state.busy && !state.pending && !data.active_run) {
      state.missingPolls += 1;
      if (state.missingPolls >= 3) {
        const runs = await (await api("/api/runs")).json();
        const saved = runs.find((run) => run.id === state.id);
        if (saved?.has_report) handleEvent("done", {total_seconds: saved.total_seconds});
        else handleEvent("error", {stage: state.currentStage || "interrupted run", total_seconds: (performance.now() - state.start) / 1000, error: "The server restarted or the run was interrupted. Retry the research, or open a completed report in Past runs."});
      }
    } else state.missingPolls = 0;
    updateRunButton(); return data;
  } catch { state.ready = false; updateRunButton(); $("server-status").textContent = "Server unavailable"; notify("The local server is unavailable. Double-click the launcher to restart it.", true); }
}
$("company-search").addEventListener("input", () => { if (!state.busy) { state.selected = null; updateRunHelp(); updateRunButton(); } renderCompanies(); });
$("run-button").addEventListener("click", runResearch); $("retry-button").addEventListener("click", runResearch);
$("history-button").addEventListener("click", showHistory); $("close-history").addEventListener("click", () => $("history-drawer").close());
$("verify-button").addEventListener("click", verifyLedger); $("pdf-button").addEventListener("click", downloadPdf);
$("show-report").addEventListener("click", () => showResult(state.id, $("run-title").textContent));
$("new-run").addEventListener("click", () => { sessionStorage.removeItem("signal-court-report"); showLive(); if (!state.busy) resetIdle(); notify(""); $("company-search").focus(); });
document.addEventListener("keydown", async (event) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (event.key === "/" && !typing && !$("history-drawer").open) { event.preventDefault(); showLive(); $("company-search").focus(); }
  if (event.key === "Enter" && !$("history-drawer").open && !document.body.classList.contains("results") && (!["BUTTON", "A"].includes(document.activeElement.tagName) || document.activeElement.classList.contains("company-option"))) { event.preventDefault(); await runResearch(); }
});
(async () => {
  await Promise.all([loadCompanies(), pollStatus()]);
  if (!state.busy) {
    try { const saved = JSON.parse(sessionStorage.getItem("signal-court-report")); if (saved?.id) showResult(saved.id, saved.company); } catch { /* A stale browser preference must not block the console. */ }
  }
  setInterval(pollStatus, 2000);
})();
