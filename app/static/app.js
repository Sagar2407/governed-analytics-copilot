"use strict";
// All data-derived content is rendered via textContent / DOM APIs (never innerHTML),
// so nothing from the API can inject markup. No inline handlers (CSP: default-src 'self').

const els = {
  persona: document.getElementById("persona"),
  asOf: document.getElementById("as-of"),
  banner: document.getElementById("scope-banner"),
  form: document.getElementById("ask-form"),
  question: document.getElementById("question"),
  askBtn: document.getElementById("ask-btn"),
  examples: document.getElementById("examples"),
  result: document.getElementById("result"),
  status: document.getElementById("status"),
  answer: document.getElementById("answer"),
  rationale: document.getElementById("rationale"),
  candidates: document.getElementById("candidates"),
  tableWrap: document.getElementById("table-wrap"),
  lineage: document.getElementById("lineage"),
  lineageBody: document.getElementById("lineage-body"),
  sql: document.getElementById("sql"),
};

const state = { scopes: {}, labels: {}, lastQuestion: "" };

const EXAMPLES = [
  "What is our ARR?",
  "ARR by segment",
  "bookings by quarter over the last 12 months",
  "new business win rate",
  "active customers in EMEA",
  "net revenue last quarter",
  "show me revenue",
  "why did churn go up?",
];

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function chip(text, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "chip";
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}

function fmt(kind, v) {
  if (typeof v !== "number") return String(v);
  if (kind === "currency") return "$" + Math.round(v).toLocaleString();
  if (kind === "ratio") return (v * 100).toFixed(1) + "%";
  return v.toLocaleString();
}

async function init() {
  try {
    const health = await (await fetch("/api/health")).json();
    if (health.as_of) els.asOf.textContent = "as of " + health.as_of;

    const metrics = (await (await fetch("/api/metrics")).json()).metrics || [];
    metrics.forEach(m => { state.labels[m.name] = m.label; });

    const personas = (await (await fetch("/api/personas")).json()).personas || [];
    personas.forEach(p => {
      state.scopes[p.persona_id] = { label: p.label, scope: p.scope_summary };
      const opt = document.createElement("option");
      opt.value = p.persona_id;
      opt.textContent = p.label;
      if (p.is_default) opt.selected = true;
      els.persona.appendChild(opt);
    });
    updateBanner();

    EXAMPLES.forEach(q => els.examples.appendChild(chip(q, () => {
      els.question.value = q;
      ask(q);
    })));
  } catch (e) {
    els.banner.hidden = false;
    els.banner.textContent = "Could not reach the API. Is the server running?";
  }

  els.form.addEventListener("submit", ev => {
    ev.preventDefault();
    const q = els.question.value.trim();
    if (q) ask(q);
  });
  els.persona.addEventListener("change", () => {
    updateBanner();
    if (state.lastQuestion) ask(state.lastQuestion);   // re-run -> RLS visibly changes
  });
}

function updateBanner() {
  const s = state.scopes[els.persona.value];
  if (!s) return;
  els.banner.hidden = false;
  clear(els.banner);
  const strong = document.createElement("strong");
  strong.textContent = s.label;
  els.banner.append("Viewing as ", strong,
    ` — scope: ${s.scope}. Row-level security is enforced server-side; the same question `,
    "returns only what this persona is entitled to see.");
}

async function ask(question) {
  state.lastQuestion = question;
  els.askBtn.disabled = true;
  els.askBtn.textContent = "…";
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, persona: els.persona.value }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      return render({ status: "error", message: err.detail || "Request failed." });
    }
    render(await res.json());
  } catch (e) {
    render({ status: "error", message: "Network error." });
  } finally {
    els.askBtn.disabled = false;
    els.askBtn.textContent = "Ask";
  }
}

function render(resp) {
  els.result.hidden = false;
  clear(els.candidates);
  clear(els.tableWrap);
  clear(els.lineageBody);
  els.sql.textContent = "";
  els.rationale.textContent = "";
  els.lineage.hidden = true;

  els.status.className = "status " + resp.status;
  els.answer.className = "answer";

  if (resp.status === "ok") {
    els.status.textContent = "Answer";
    const grouped = resp.lineage && resp.lineage.grouped_by && resp.lineage.grouped_by.length;
    if (grouped) {
      els.answer.classList.add("message");
      els.answer.textContent = resp.answer;
      renderTable(resp);
    } else {
      els.answer.textContent = resp.answer;
    }
    els.rationale.textContent = resp.rationale || "";
    renderLineage(resp);
  } else if (resp.status === "clarify") {
    els.status.textContent = "Needs clarification";
    els.answer.classList.add("message");
    els.answer.textContent = resp.message || "Could you clarify?";
    (resp.candidates || []).forEach(name => {
      const label = state.labels[name] || name;
      els.candidates.appendChild(chip(label, () => {
        els.question.value = label;
        ask(label);
      }));
    });
  } else if (resp.status === "abstain") {
    els.status.textContent = "Can’t answer that";
    els.answer.classList.add("message");
    els.answer.textContent = resp.message || "Out of scope.";
  } else {
    els.status.textContent = "Error";
    els.answer.classList.add("message");
    els.answer.textContent = resp.message || "Something went wrong.";
  }
}

function renderTable(resp) {
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  resp.columns.forEach(c => {
    const th = document.createElement("th");
    th.textContent = c;
    if (c === "value") th.className = "num";
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  resp.rows.forEach(row => {
    const tr = document.createElement("tr");
    resp.columns.forEach(c => {
      const td = document.createElement("td");
      if (c === "value") {
        td.className = "num";
        td.textContent = fmt(resp.value_kind, row[c]);
      } else {
        td.textContent = row[c] === null || row[c] === undefined ? "—" : String(row[c]);
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  els.tableWrap.appendChild(table);
}

function renderLineage(resp) {
  els.lineage.hidden = false;
  const L = resp.lineage || {};
  const scopes = (L.principal && L.principal.scopes) || [];
  const rows = [
    ["Metric", L.metric_label || (resp.plan && resp.plan.metric)],
    ["Aggregation", L.aggregation],
    ["Time window", L.time_window],
    ["Grouped by", (L.grouped_by || []).join(", ") || "—"],
    ["Filters", (L.filters || []).join("; ") || "—"],
    ["Eligibility", L.eligibility],
    ["Scope (RLS)", scopes.join(", ") + (L.principal && L.principal.unrestricted ? " (unrestricted)" : "")],
    ["Currency", L.currency || "—"],
  ];
  rows.forEach(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v === undefined || v === null ? "—" : String(v);
    els.lineageBody.appendChild(dt);
    els.lineageBody.appendChild(dd);
  });
  els.sql.textContent = resp.sql || "";
}

init();
