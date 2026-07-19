"use strict";

// The CSRF token is intentionally kept only in memory. The session credential
// remains in the HttpOnly cookie and is never visible to this script.
const state = { csrf: null };
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
    output.textContent = JSON.stringify(body.snapshot, null, 2);
  } catch (error) {
    card.classList.add("error");
    output.textContent = `Unavailable\n\n${error.message}`;
    throw error;
  }
}

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
