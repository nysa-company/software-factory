// Relay — conformance product. Zero dependencies; Node 18+.
// See ../SPEC.md for the behavior contract this implements.
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const PORT = Number(process.env.PORT || 4700);
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "data");
const STATE_FILE = path.join(DATA_DIR, "state.json");
const ALLOWLIST = (process.env.ALLOWLIST || "test@example.com").split(",").map(s => s.trim());
const WORKER_MS = Number(process.env.WORKER_MS || 200);
const MAX_ATTEMPTS = 3;

// ---------- durable state ----------
let state = { events: [], jobs: [], approvals: [], outbox: [] };

function loadState() {
  try {
    state = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  } catch {
    /* first boot or unreadable: start clean */
  }
}

// Atomic persist: write temp file, then rename. A crash mid-write never
// corrupts the previous good state.
function persist() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const tmp = STATE_FILE + "." + crypto.randomBytes(4).toString("hex");
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
  fs.renameSync(tmp, STATE_FILE);
}

// ---------- domain ----------
function acceptEvent(evt) {
  if (!evt || typeof evt.id !== "string" || !evt.id) return { ok: false, error: "missing event id" };
  const existing = state.events.find(e => e.id === evt.id);
  if (existing) return { ok: true, duplicate: true, eventId: evt.id }; // idempotent ack
  state.events.push({ id: evt.id, type: evt.type || "generic", payload: evt.payload || {}, receivedAt: new Date().toISOString() });
  state.jobs.push({
    id: "job-" + evt.id,
    eventId: evt.id,
    status: "pending", // pending | done | dead
    attempts: 0,
    lastError: null,
  });
  persist();
  return { ok: true, duplicate: false, eventId: evt.id };
}

function processJob(job) {
  const evt = state.events.find(e => e.id === job.eventId);
  job.attempts += 1;
  // Simulated transient/permanent failure: payload.failTimes = number of
  // attempts that must fail before success (99 = effectively permanent).
  const failTimes = Number(evt?.payload?.failTimes || 0);
  if (job.attempts <= failTimes) {
    job.lastError = `simulated failure on attempt ${job.attempts}`;
    if (job.attempts >= MAX_ATTEMPTS) job.status = "dead";
    persist();
    return;
  }
  // Success: produce a proposed action requiring approval. Nothing sends here.
  state.approvals.push({
    id: "appr-" + job.eventId,
    jobId: job.id,
    action: {
      to: evt?.payload?.to || ALLOWLIST[0],
      subject: evt?.payload?.subject || `Follow-up for ${job.eventId}`,
      body: evt?.payload?.body || "Proposed by Relay worker.",
    },
    status: "pending", // pending | sent | rejected | blocked_recipient
    proposedAt: new Date().toISOString(),
  });
  job.status = "done";
  job.lastError = null;
  persist();
}

function workerTick() {
  const job = state.jobs.find(j => j.status === "pending");
  if (job) processJob(job);
}

function approve(id) {
  const appr = state.approvals.find(a => a.id === id);
  if (!appr) return { ok: false, code: 404, error: "no such approval" };
  if (appr.status !== "pending") return { ok: false, code: 409, error: `already ${appr.status}` };
  if (!ALLOWLIST.includes(appr.action.to)) {
    appr.status = "blocked_recipient";
    persist();
    return { ok: false, code: 403, error: `recipient ${appr.action.to} not on allowlist — send blocked`, status: appr.status };
  }
  state.outbox.push({ ...appr.action, approvalId: appr.id, sentAt: new Date().toISOString() });
  appr.status = "sent";
  persist();
  return { ok: true, status: "sent" };
}

function reject(id) {
  const appr = state.approvals.find(a => a.id === id);
  if (!appr) return { ok: false, code: 404, error: "no such approval" };
  if (appr.status !== "pending") return { ok: false, code: 409, error: `already ${appr.status}` };
  appr.status = "rejected";
  persist();
  return { ok: true, status: "rejected" };
}

// ---------- http ----------
function json(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", c => { data += c; if (data.length > 1e6) req.destroy(); });
    req.on("end", () => { try { resolve(data ? JSON.parse(data) : {}); } catch (e) { reject(e); } });
    req.on("error", reject);
  });
}

function ui() {
  const row = (cells) => `<tr>${cells.map(c => `<td>${String(c)}</td>`).join("")}</tr>`;
  return `<!doctype html><meta charset="utf-8"><title>Relay</title>
<style>body{font-family:system-ui;margin:2rem;max-width:60rem}table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}td,th{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:14px}h2{margin:.2rem 0 .4rem}</style>
<h1>Relay</h1>
<h2>Events (${state.events.length})</h2><table><tr><th>id</th><th>type</th><th>received</th></tr>${state.events.map(e => row([e.id, e.type, e.receivedAt])).join("")}</table>
<h2>Jobs</h2><table><tr><th>id</th><th>status</th><th>attempts</th><th>lastError</th></tr>${state.jobs.map(j => row([j.id, j.status, j.attempts, j.lastError || ""])).join("")}</table>
<h2>Approvals</h2><table><tr><th>id</th><th>to</th><th>subject</th><th>status</th></tr>${state.approvals.map(a => row([a.id, a.action.to, a.action.subject, a.status])).join("")}</table>
<h2>Outbox (sandboxed)</h2><table><tr><th>to</th><th>subject</th><th>sentAt</th></tr>${state.outbox.map(o => row([o.to, o.subject, o.sentAt])).join("")}</table>`;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  try {
    if (req.method === "GET" && url.pathname === "/") {
      res.writeHead(200, { "Content-Type": "text/html" });
      return res.end(ui());
    }
    if (req.method === "GET" && url.pathname === "/api/state") return json(res, 200, state);
    if (req.method === "GET" && url.pathname === "/health") return json(res, 200, { ok: true });
    if (req.method === "POST" && url.pathname === "/webhook/event") {
      const body = await readBody(req);
      const result = acceptEvent(body);
      return json(res, result.ok ? 200 : 400, result);
    }
    const mApprove = url.pathname.match(/^\/api\/approvals\/([^/]+)\/approve$/);
    if (req.method === "POST" && mApprove) {
      const result = approve(mApprove[1]);
      return json(res, result.ok ? 200 : result.code, result);
    }
    const mReject = url.pathname.match(/^\/api\/approvals\/([^/]+)\/reject$/);
    if (req.method === "POST" && mReject) {
      const result = reject(mReject[1]);
      return json(res, result.ok ? 200 : result.code, result);
    }
    json(res, 404, { error: "not found" });
  } catch (e) {
    json(res, 400, { error: String(e.message || e) });
  }
});

loadState();
setInterval(workerTick, WORKER_MS);
server.listen(PORT, () => console.log(`relay listening on :${PORT} (data: ${DATA_DIR})`));
