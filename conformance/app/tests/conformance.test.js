// Conformance suite for Relay — the kit's permanent test bed.
// Runs the real server as a child process (crash recovery demands it).
// npm test  (= node --test tests/conformance.test.js)
const { test, before, after } = require("node:test");
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const PORT = 4719;
const BASE = `http://localhost:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "relay-test-"));
const SERVER = path.join(__dirname, "..", "server.js");
let proc;

function startServer() {
  proc = spawn(process.execPath, [SERVER], {
    env: { ...process.env, PORT: String(PORT), DATA_DIR, WORKER_MS: "50", ALLOWLIST: "test@example.com" },
    stdio: "ignore",
  });
}

async function waitForHealth(tries = 50) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(`${BASE}/health`);
      if (r.ok) return;
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error("server did not come up");
}

const post = (p, body) => fetch(BASE + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const getState = async () => (await fetch(`${BASE}/api/state`)).json();
const settle = async (pred, tries = 60) => {
  for (let i = 0; i < tries; i++) {
    const s = await getState();
    if (pred(s)) return s;
    await new Promise(r => setTimeout(r, 100));
  }
  return getState();
};

before(async () => { startServer(); await waitForHealth(); });
after(() => { proc?.kill("SIGKILL"); fs.rmSync(DATA_DIR, { recursive: true, force: true }); });

test("1. walking skeleton: event in, visible in state", async () => {
  const r = await post("/webhook/event", { id: "e1", type: "meeting", payload: {} });
  assert.strictEqual((await r.json()).ok, true);
  const s = await settle(x => x.events.some(e => e.id === "e1"));
  assert.ok(s.events.find(e => e.id === "e1"));
});

test("2. duplicate event id creates exactly one job", async () => {
  await post("/webhook/event", { id: "e2", payload: {} });
  const dup = await (await post("/webhook/event", { id: "e2", payload: {} })).json();
  assert.strictEqual(dup.duplicate, true);
  const s = await getState();
  assert.strictEqual(s.jobs.filter(j => j.eventId === "e2").length, 1);
});

test("3. transient failure retries then succeeds, attempts recorded", async () => {
  await post("/webhook/event", { id: "e3", payload: { failTimes: 2 } });
  const s = await settle(x => x.jobs.find(j => j.eventId === "e3")?.status === "done");
  const job = s.jobs.find(j => j.eventId === "e3");
  assert.strictEqual(job.status, "done");
  assert.strictEqual(job.attempts, 3);
});

test("4. permanent failure dead-letters after 3 attempts", async () => {
  await post("/webhook/event", { id: "e4", payload: { failTimes: 99 } });
  const s = await settle(x => x.jobs.find(j => j.eventId === "e4")?.status === "dead");
  const job = s.jobs.find(j => j.eventId === "e4");
  assert.strictEqual(job.status, "dead");
  assert.strictEqual(job.attempts, 3);
  assert.match(job.lastError, /simulated failure/);
});

test("5. approval gate: nothing sends before approve, sends after", async () => {
  await post("/webhook/event", { id: "e5", payload: { to: "test@example.com", subject: "hi" } });
  let s = await settle(x => x.approvals.some(a => a.id === "appr-e5"));
  assert.strictEqual(s.outbox.filter(o => o.approvalId === "appr-e5").length, 0, "outbox must be empty pre-approval");
  const r = await (await post("/api/approvals/appr-e5/approve")).json();
  assert.strictEqual(r.ok, true);
  s = await getState();
  assert.strictEqual(s.outbox.filter(o => o.approvalId === "appr-e5").length, 1);
  // idempotent guard: second approve is refused
  const again = await post("/api/approvals/appr-e5/approve");
  assert.strictEqual(again.status, 409);
});

test("6. allowlist: approved send to unlisted recipient is blocked, not sent", async () => {
  await post("/webhook/event", { id: "e6", payload: { to: "stranger@evil.com" } });
  await settle(x => x.approvals.some(a => a.id === "appr-e6"));
  const r = await post("/api/approvals/appr-e6/approve");
  assert.strictEqual(r.status, 403);
  const s = await getState();
  assert.strictEqual(s.approvals.find(a => a.id === "appr-e6").status, "blocked_recipient");
  assert.strictEqual(s.outbox.filter(o => o.approvalId === "appr-e6").length, 0);
});

test("6b. reject: closes the proposal, nothing sends", async () => {
  await post("/webhook/event", { id: "e6b", payload: { to: "test@example.com" } });
  await settle(x => x.approvals.some(a => a.id === "appr-e6b"));
  const r = await (await post("/api/approvals/appr-e6b/reject")).json();
  assert.strictEqual(r.status, "rejected");
  const s = await getState();
  assert.strictEqual(s.outbox.filter(o => o.approvalId === "appr-e6b").length, 0);
  // approve after reject is refused
  assert.strictEqual((await post("/api/approvals/appr-e6b/approve")).status, 409);
});

test("7. crash recovery: SIGKILL mid-flight loses nothing, duplicates nothing", async () => {
  // enqueue retrying work and wait until it is PROVABLY mid-flight:
  // attempted at least once, not yet done. Killing before any attempt (or
  // after completion) would not exercise recovery.
  await post("/webhook/event", { id: "e7", payload: { failTimes: 2, to: "test@example.com" } });
  // tight poll (10ms): the pending-with-attempts window is only ~2 worker ticks wide
  let midJob = null;
  for (let i = 0; i < 300; i++) {
    const s = await getState();
    const j = s.jobs.find(j => j.eventId === "e7");
    if (j && j.attempts >= 1 && j.status === "pending") { midJob = j; break; }
    if (j && j.status !== "pending") break; // completed before we caught it
    await new Promise(r => setTimeout(r, 10));
  }
  assert.ok(midJob, "job must be observed mid-flight (attempted, not done) at kill time");
  proc.kill("SIGKILL"); // no cleanup, no flush — the hard case
  await new Promise(r => setTimeout(r, 200));

  startServer();
  await waitForHealth();

  const s = await settle(x => x.jobs.find(j => j.eventId === "e7")?.status === "done");
  assert.strictEqual(s.jobs.find(j => j.eventId === "e7").status, "done", "pending job resumed after crash");
  // everything from earlier tests survived
  assert.ok(s.events.find(e => e.id === "e1"), "pre-crash events survived");
  assert.strictEqual(s.outbox.filter(o => o.approvalId === "appr-e5").length, 1, "no duplicate sends after restart");
  // duplicate delivery after restart still refused
  const dup = await (await post("/webhook/event", { id: "e1" })).json();
  assert.strictEqual(dup.duplicate, true);
});
