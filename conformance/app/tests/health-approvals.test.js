const { test } = require("node:test");
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const SERVER = path.join(__dirname, "..", "server.js");

async function withServer(port, run) {
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-health-approvals-test-"));
  const proc = spawn(process.execPath, [SERVER], {
    env: {
      ...process.env,
      PORT: String(port),
      DATA_DIR: dataDir,
      WORKER_MS: "50",
      ALLOWLIST: "test@example.com",
    },
    stdio: "ignore",
  });

  try {
    for (let i = 0; i < 50; i++) {
      try {
        if ((await fetch(`${base}/health`)).ok) break;
      } catch { /* not up yet */ }
      if (i === 49) throw new Error("server did not come up");
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    await run(base);
  } finally {
    proc.kill("SIGKILL");
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
}

async function post(base, pathname, body = {}) {
  return fetch(base + pathname, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function waitForApproval(base, id, tries = 60) {
  for (let i = 0; i < tries; i++) {
    const state = await (await fetch(`${base}/api/state`)).json();
    if (state.approvals.some(approval => approval.id === id)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`approval ${id} was not proposed`);
}

async function assertHealth(base, expected) {
  const response = await fetch(`${base}/health`);
  assert.strictEqual(response.status, 200);
  assert.strictEqual(response.headers.get("content-type"), "application/json");
  assert.strictEqual(await response.text(), JSON.stringify(expected));
}

test("1. Fresh server", async () => {
  await withServer(4731, async base => {
    await assertHealth(base, {
      ok: true,
      queue: { pending: 0, done: 0, dead: 0 },
      approvals: { pending: 0, sent: 0, rejected: 0, blocked_recipient: 0 },
    });
  });
});

test("2. One pending approval", async () => {
  await withServer(4732, async base => {
    await post(base, "/webhook/event", {
      id: "health-one-pending",
      payload: { to: "test@example.com" },
    });
    await waitForApproval(base, "appr-health-one-pending");

    await assertHealth(base, {
      ok: true,
      queue: { pending: 0, done: 1, dead: 0 },
      approvals: { pending: 1, sent: 0, rejected: 0, blocked_recipient: 0 },
    });
  });
});

test("3. Approved", async () => {
  await withServer(4733, async base => {
    await post(base, "/webhook/event", {
      id: "health-approved",
      payload: { to: "test@example.com" },
    });
    await waitForApproval(base, "appr-health-approved");

    const approval = await post(base, "/api/approvals/appr-health-approved/approve");
    assert.strictEqual(approval.status, 200);

    await assertHealth(base, {
      ok: true,
      queue: { pending: 0, done: 1, dead: 0 },
      approvals: { pending: 0, sent: 1, rejected: 0, blocked_recipient: 0 },
    });
  });
});
