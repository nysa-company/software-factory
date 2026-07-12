const { test, before, after } = require("node:test");
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const PORT = 4721;
const BASE = `http://localhost:${PORT}`;
const DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "relay-health-test-"));
const SERVER = path.join(__dirname, "..", "server.js");
let proc;

const post = (body) => fetch(`${BASE}/webhook/event`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const getHealth = async () => (await fetch(`${BASE}/health`)).json();
const getState = async () => (await fetch(`${BASE}/api/state`)).json();

async function settle(pred, tries = 60) {
  for (let i = 0; i < tries; i++) {
    const state = await getState();
    if (pred(state)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("job did not settle");
}

before(async () => {
  proc = spawn(process.execPath, [SERVER], {
    env: { ...process.env, PORT: String(PORT), DATA_DIR, WORKER_MS: "50" },
    stdio: "ignore",
  });
  for (let i = 0; i < 50; i++) {
    try {
      if ((await fetch(`${BASE}/health`)).ok) return;
    } catch { /* not up yet */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("server did not come up");
});

after(() => {
  proc?.kill("SIGKILL");
  fs.rmSync(DATA_DIR, { recursive: true, force: true });
});

test("1. on a fresh server, /health reports an empty queue", async () => {
  assert.deepStrictEqual(await getHealth(), {
    ok: true,
    queue: { pending: 0, done: 0, dead: 0 },
  });
});

test("2. after a job completes, /health reports queue.done incremented by 1", async () => {
  await post({ id: "health-done", payload: {} });
  await settle(state => state.jobs.find(job => job.eventId === "health-done")?.status === "done");

  assert.deepStrictEqual(await getHealth(), {
    ok: true,
    queue: { pending: 0, done: 1, dead: 0 },
  });
});

test("3. after a job exhausts retries, /health reports queue.dead incremented by 1", async () => {
  await post({ id: "health-dead", payload: { failTimes: 99 } });
  await settle(state => state.jobs.find(job => job.eventId === "health-dead")?.status === "dead");

  assert.deepStrictEqual(await getHealth(), {
    ok: true,
    queue: { pending: 0, done: 1, dead: 1 },
  });
});
