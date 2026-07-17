const LAYERS = [
  ["units", "Units"],
  ["threads", "Threads"],
  ["features", "Features"],
  ["products", "Products"],
  ["subprojects", "Sub-projects"],
  ["projects", "Projects"],
];

const state = {
  graph: null,
  parents: new Map(),   // node id -> Set of container ids (one layer up / nesting)
  children: new Map(),  // node id -> Set of member ids
  byId: new Map(),
  selected: null,
  hits: new Set(),
};

function addLink(from, to) {
  if (!state.children.has(from)) state.children.set(from, new Set());
  state.children.get(from).add(to);
  if (!state.parents.has(to)) state.parents.set(to, new Set());
  state.parents.get(to).add(from);
}

async function loadGraph() {
  const res = await fetch("/api/graph");
  state.graph = await res.json();
  const { layers, links } = state.graph;
  for (const [key] of LAYERS) {
    for (const node of layers[key]) state.byId.set(node.id, { ...node, layer: key });
  }
  for (const group of Object.values(links)) {
    for (const { from, to } of group) addLink(from, to);
  }
  render();
  document.getElementById("stats").textContent =
    LAYERS.map(([k, label]) => `${layers[k].length} ${label.toLowerCase()}`).join(" · ");
}

function lineageOf(id) {
  const seen = new Set([id]);
  const walk = (start, map) => {
    const queue = [start];
    while (queue.length) {
      for (const next of map.get(queue.pop()) || []) {
        if (!seen.has(next)) { seen.add(next); queue.push(next); }
      }
    }
  };
  walk(id, state.parents);
  walk(id, state.children);
  return seen;
}

function render() {
  const container = document.getElementById("columns");
  container.textContent = "";
  const filter = document.getElementById("search").value.trim().toLowerCase();
  const lineage = state.selected ? lineageOf(state.selected) : null;

  for (const [key, label] of LAYERS) {
    const column = document.createElement("div");
    column.className = "column";
    const title = document.createElement("h3");
    title.textContent = label;
    column.appendChild(title);
    const nodes = document.createElement("div");
    nodes.className = "nodes";
    for (const node of state.graph.layers[key]) {
      if (filter && !(node.text || "").toLowerCase().includes(filter)) continue;
      const el = document.createElement("div");
      el.className = "node";
      el.dataset.id = node.id;
      el.textContent = node.text || node.id;
      el.title = node.text || node.id;
      if (node.polarity === "negative" || node.reality_polarity === "negative") el.classList.add("negative");
      if (state.hits.has(node.id)) el.classList.add("hit");
      if (state.selected === node.id) el.classList.add("selected");
      else if (lineage) el.classList.add(lineage.has(node.id) ? "lineage" : "dimmed");
      el.onclick = () => select(node.id);
      nodes.appendChild(el);
    }
    column.appendChild(nodes);
    container.appendChild(column);
  }
}

function select(id) {
  state.selected = state.selected === id ? null : id;
  render();
  renderDetail();
}

function renderDetail() {
  const panel = document.getElementById("detail");
  const node = state.selected ? state.byId.get(state.selected) : null;
  if (!node) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const rows = [];
  const add = (label, value) => {
    if (value === undefined || value === null || value === "") return;
    rows.push(`<dt>${label}</dt><dd>${escapeHtml(String(value))}</dd>`);
  };
  add("Layer", node.layer);
  add("Id", node.id);
  add("Text", node.text);
  add("Kind", node.kind);
  add("Polarity", node.polarity || node.reality_polarity);
  add("Strength", node.strength);
  add("Status", node.status);
  add("Source", node.source);
  add("Session", node.sid);
  add("Timestamp", node.ts);
  add("User seq", node.user_seq);
  add("Cwd", node.cwd || (node.project_cwds || []).join(", "));
  if (node.edited_files && node.edited_files.length) add("Edited files", node.edited_files.join(", "));
  let html = `<dl>${rows.join("")}</dl>`;
  if (node.source_text) {
    html += `<dt>Originating user prompt (${escapeHtml(node.source_prompt_key || "")})</dt>
             <div class="prompt">${escapeHtml(node.source_text)}</div>`;
  }
  panel.innerHTML = html;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ── query panel ──────────────────────────────────────────────────────
const statusEl = () => document.getElementById("query-status");

async function fireQuery() {
  const query = document.getElementById("query-input").value.trim();
  if (!query) return;
  const button = document.getElementById("query-fire");
  button.disabled = true;
  setStatus("firing…", "pending");
  try {
    const res = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        cwd: document.getElementById("query-cwd").value.trim(),
        all_projects: document.getElementById("query-all").checked,
      }),
    });
    const fired = await res.json();
    if (!res.ok || fired.success === false) throw new Error(fired.detail || fired.error || "fire failed");
    setStatus("processing…", "pending");
    await pollResults(fired.id);
  } catch (err) {
    setStatus(String(err.message || err), "error");
  } finally {
    button.disabled = false;
  }
}

async function pollResults(id) {
  for (;;) {
    const res = await fetch(`/api/query/${encodeURIComponent(id)}`);
    const data = await res.json();
    if (!res.ok || data.success === false) throw new Error(data.detail || data.error || "results failed");
    if (data.ready) { showResults(data); return; }
    setStatus(`processing… (${data.status || "running"})`, "pending");
  }
}

function extractRequirements(result) {
  const text = result?.result?.text ?? result?.text ?? "";
  if (typeof text !== "string" || !text) return [];
  const start = text.indexOf("{");
  for (let i = start; i >= 0 && i < text.length; i = text.indexOf("{", i + 1)) {
    try {
      const parsed = JSON.parse(text.slice(i, text.lastIndexOf("}") + 1));
      if (Array.isArray(parsed.requirements)) return parsed.requirements;
    } catch { /* keep scanning */ }
  }
  return [];
}

function showResults(data) {
  const requirements = extractRequirements(data);
  setStatus(`done — ${requirements.length} requirement(s)`, "idle");
  state.hits = new Set(
    requirements.map(r => r?.evidence?.unit_source_key).filter(Boolean)
  );
  const list = document.getElementById("query-list");
  list.textContent = "";
  for (const req of requirements) {
    const el = document.createElement("div");
    el.className = "result";
    const unitKey = req?.evidence?.unit_source_key;
    el.innerHTML = `
      <div>${escapeHtml(req.text || "")}</div>
      <div class="meta">${escapeHtml([req.kind, req.origin, req.strength, req.cwd].filter(Boolean).join(" · "))}
      ${unitKey && state.byId.has(unitKey) ? ` · <span class="jump" data-id="${escapeHtml(unitKey)}">show unit</span>` : ""}</div>`;
    const jump = el.querySelector(".jump");
    if (jump) jump.onclick = () => {
      select(jump.dataset.id);
      document.querySelector(`.node[data-id="${CSS.escape(jump.dataset.id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    };
    list.appendChild(el);
  }
  if (!requirements.length) {
    list.innerHTML = `<div class="result">No requirements parsed. Raw text:<div class="prompt">${escapeHtml(String(data?.result?.text ?? data?.text ?? ""))}</div></div>`;
  }
  document.getElementById("query-results").classList.remove("hidden");
  render();
}

function setStatus(text, cls) {
  const el = statusEl();
  el.textContent = text;
  el.className = cls;
}

document.getElementById("query-fire").onclick = fireQuery;
document.getElementById("query-input").addEventListener("keydown", e => { if (e.key === "Enter") fireQuery(); });
document.getElementById("search").addEventListener("input", render);
document.getElementById("query-close").onclick = () => {
  document.getElementById("query-results").classList.add("hidden");
  state.hits = new Set();
  render();
};

loadGraph();
