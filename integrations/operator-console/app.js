"use strict";

// The CSRF token is intentionally kept only in memory. The session credential
// remains in the HttpOnly cookie and is never visible to this script.
const state = { csrf: null, snapshots: {}, previews: {} };
const project = document.querySelector("#project");
const refresh = document.querySelector("#refresh");
const status = document.querySelector("#status");
const statusDot = document.querySelector("#status-dot");
const updated = document.querySelector("#updated");
const views = ["workflow", "model", "envelope", "spend"];

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error?.message || `Request failed (${response.status})`);
  }
  return body;
}

async function mutate(action, payload) {
  const response = await api(`/api/actions/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": state.csrf,
    },
    body: JSON.stringify({ project: project.value, ...payload }),
  });
  return response.result;
}

function setStatus(message, error = false) {
  status.textContent = message;
  statusDot.classList.toggle("error", error);
}

async function loadView(view, selected) {
  const card = document.querySelector(`[data-view="${view}"]`);
  const output = card.querySelector("pre");
  card.classList.remove("error");
  output.textContent = "Loading…";
  try {
    const body = await api(`/api/snapshots/${view}?project=${encodeURIComponent(selected)}`);
    state.snapshots[view] = body.snapshot;
    output.textContent = JSON.stringify(body.snapshot, null, 2);
    if (view === "workflow" || view === "model") renderPolicy();
    if (view === "envelope") renderEnvelope();
  } catch (error) {
    card.classList.add("error");
    output.textContent = `Unavailable\n\n${error.message}`;
    throw error;
  }
}

function select(label, name, values, selected) {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const element = document.createElement("select");
  element.dataset.name = name;
  element.replaceChildren(...values.map((value) => {
    const option = document.createElement("option");
    option.value = value.route_id || value;
    option.textContent = value.route_id
      ? `${value.selection_id} · ${value.adapter}`
      : value;
    option.selected = option.value === selected;
    return option;
  }));
  wrapper.append(element);
  return wrapper;
}

function renderPolicy() {
  const workflow = state.snapshots.workflow;
  const candidates = state.snapshots.model;
  if (!workflow?.roles || !candidates?.routes) return;
  const editor = document.querySelector("#model-policy-editor");
  const current = candidates.current_policy;
  editor.replaceChildren(...workflow.roles.map((item) => {
    const row = document.createElement("div");
    row.className = "policy-row";
    row.dataset.role = item.role;
    const role = document.createElement("strong");
    role.textContent = item.role;
    const family = current
      ? (item.lane.toLowerCase() === "production"
        ? current.production_family : current.checking_family)
      : item.primary.family;
    const routes = candidates.routes.filter((route) => route.provider_family === family);
    const value = current?.roles[item.role] || {
      primary_route_id: item.primary.route_id,
      secondary_route_id: item.secondary.route_id,
      effort: item.effort,
    };
    row.append(
      role,
      select("Primary", "primary", routes, value.primary_route_id),
      select("Secondary", "secondary", routes, value.secondary_route_id),
      select("Effort", "effort", candidates.efforts, value.effort),
    );
    const primary = row.querySelector('[data-name="primary"]');
    const secondary = row.querySelector('[data-name="secondary"]');
    const syncSecondary = () => {
      [...secondary.options].forEach((option) => {
        option.disabled = option.value === primary.value;
      });
      if (secondary.value === primary.value) {
        const alternative = [...secondary.options].find((option) => !option.disabled);
        if (alternative) secondary.value = alternative.value;
      }
    };
    primary.addEventListener("change", syncSecondary);
    syncSecondary();
    return row;
  }));
}

function policyPayload() {
  const workflow = state.snapshots.workflow;
  const roles = {};
  document.querySelectorAll(".policy-row").forEach((row) => {
    roles[row.dataset.role] = {
      primary_route_id: row.querySelector('[data-name="primary"]').value,
      secondary_route_id: row.querySelector('[data-name="secondary"]').value,
      effort: row.querySelector('[data-name="effort"]').value,
    };
  });
  const production = workflow.roles.find((value) => value.lane.toLowerCase() === "production");
  const checking = workflow.roles.find((value) => value.lane.toLowerCase() === "checking");
  return {
    schema: "factory-model-policy/v1",
    version: 1,
    production_family: production.primary.family,
    checking_family: checking.primary.family,
    roles,
  };
}

function renderEnvelope() {
  const values = state.snapshots.envelope?.values;
  if (!values) return;
  document.querySelector("#cap-run").value = values.PER_RUN_BUDGET_USD || "";
  document.querySelector("#cap-ticket").value = values.PER_TICKET_BUDGET_USD || "";
  document.querySelector("#cap-daily").value = values.DAILY_CAP_USD || "";
  document.querySelector("#cap-builder").value =
    values.BUILDER_PER_RUN_BUDGET_USD || values.PER_RUN_BUDGET_USD || "";
}

function changes(ids) {
  return Object.fromEntries(ids.map(([id, key]) => {
    const value = document.querySelector(id).value.trim();
    return [key, value];
  }).filter(([, value]) => value));
}

async function previewApply(kind, previewAction, applyAction, payload, output, applyExtras = {}) {
  const target = document.querySelector(output);
  const apply = document.querySelector(`#${kind}-apply`);
  apply.disabled = true;
  try {
    const result = await mutate(previewAction, payload);
    state.previews[kind] = { payload, hash: result.preview_hash };
    target.textContent = JSON.stringify(result, null, 2);
    apply.disabled = false;
  } catch (error) {
    target.textContent = error.message;
  }
  apply.onclick = async () => {
    apply.disabled = true;
    try {
      const preview = state.previews[kind];
      const result = await mutate(applyAction, {
        ...preview.payload,
        ...applyExtras,
        approve_hash: preview.hash,
      });
      target.textContent = JSON.stringify(result, null, 2);
      await loadSnapshots();
    } catch (error) {
      target.textContent = error.message;
    }
  };
}

document.querySelector("#model-preview").addEventListener("click", () => {
  previewApply(
    "model", "model-policy-preview", "model-policy-apply",
    {
      policy: policyPayload(),
    },
    "#model-result",
    { expected_current_hash: state.snapshots.model.current_policy_hash },
  );
});

document.querySelector("#envelope-preview").addEventListener("click", () => {
  previewApply(
    "envelope", "envelope-plan", "envelope-apply",
    { changes: changes([
      ["#cap-run", "PER_RUN_BUDGET_USD"],
      ["#cap-ticket", "PER_TICKET_BUDGET_USD"],
      ["#cap-daily", "DAILY_CAP_USD"],
      ["#cap-builder", "BUILDER_PER_RUN_BUDGET_USD"],
    ]) },
    "#envelope-result",
  );
});

document.querySelector("#override-preview").addEventListener("click", () => {
  const issued = new Date();
  const expires = new Date(issued.getTime() + 15 * 60 * 1000);
  const utc = (value) => value.toISOString().replace(/\.[0-9]{3}Z$/, "Z");
  previewApply(
    "override", "envelope-override-plan", "envelope-override-apply",
    {
      scope: "role",
      ticket: document.querySelector("#override-ticket").value.trim() || null,
      role: document.querySelector("#override-role").value,
      day: null,
      issued_at: utc(issued),
      expires_at: utc(expires),
      operator_id: document.querySelector("#override-operator").value.trim(),
      reason: "budget_exhausted",
      changes: { PER_RUN_BUDGET_USD: document.querySelector("#override-budget").value.trim() },
    },
    "#override-result",
  );
});

document.querySelector("#cancel-preview").addEventListener("click", () => {
  previewApply(
    "cancel", "attempt-cancel-plan", "attempt-cancel",
    {
      ticket: document.querySelector("#cancel-ticket").value.trim(),
      run: document.querySelector("#cancel-run").value.trim(),
      reason: "budget_exhausted",
    },
    "#cancel-result",
  );
});

async function loadSnapshots() {
  const selected = project.value;
  if (!selected) return;
  refresh.disabled = true;
  project.disabled = true;
  setStatus(`Reading ${selected} through the trusted launcher…`);
  const results = await Promise.allSettled(views.map((view) => loadView(view, selected)));
  const failures = results.filter((result) => result.status === "rejected").length;
  setStatus(
    failures ? `${views.length - failures} of ${views.length} snapshots available` : "All snapshots current",
    failures === views.length,
  );
  updated.textContent = new Date().toLocaleTimeString();
  refresh.disabled = false;
  project.disabled = false;
}

async function start() {
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf;
    const listing = await api("/api/projects");
    project.replaceChildren(...listing.projects.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    }));
    project.disabled = false;
    refresh.disabled = false;
    await loadSnapshots();
  } catch (error) {
    setStatus(error.message, true);
  }
}

refresh.addEventListener("click", loadSnapshots);
project.addEventListener("change", loadSnapshots);
start();
