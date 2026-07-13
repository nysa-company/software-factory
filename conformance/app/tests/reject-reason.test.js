const { test } = require("node:test");
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const SERVER = path.join(__dirname, "..", "server.js");

function startServer(port, dataDir) {
  return spawn(process.execPath, [SERVER], {
    env: {
      ...process.env,
      PORT: String(port),
      DATA_DIR: dataDir,
      WORKER_MS: "50",
      ALLOWLIST: "test@example.com",
    },
    stdio: "ignore",
  });
}

async function waitForHealth(base, tries = 50) {
  for (let i = 0; i < tries; i++) {
    try {
      if ((await fetch(`${base}/health`)).ok) return;
    } catch { /* not up yet */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("server did not come up");
}

async function stopServer(proc) {
  if (proc.exitCode !== null || proc.signalCode !== null) return;
  const exited = new Promise(resolve => proc.once("exit", resolve));
  proc.kill("SIGKILL");
  await exited;
}

async function withServer(port, run) {
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-reject-reason-test-"));
  const proc = startServer(port, dataDir);

  try {
    await waitForHealth(base);
    await run({ base, dataDir, proc });
  } finally {
    await stopServer(proc);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
}

async function post(base, pathname, body, { omitBody = false } = {}) {
  const options = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  };
  if (!omitBody) options.body = JSON.stringify(body);
  return fetch(base + pathname, options);
}

async function getState(base) {
  return (await fetch(`${base}/api/state`)).json();
}

async function createApproval(base, eventId, action = {}) {
  const response = await post(base, "/webhook/event", {
    id: eventId,
    payload: {
      to: "test@example.com",
      subject: `Subject ${eventId}`,
      body: `Body ${eventId}`,
      ...action,
    },
  });
  assert.strictEqual(response.status, 200);

  const approvalId = `appr-${eventId}`;
  for (let i = 0; i < 60; i++) {
    const approval = (await getState(base)).approvals.find(item => item.id === approvalId);
    if (approval) return approval;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`approval ${approvalId} was not proposed`);
}

function expectedApproval(pendingApproval, reason) {
  return {
    id: pendingApproval.id,
    jobId: pendingApproval.jobId,
    action: pendingApproval.action,
    status: "rejected",
    proposedAt: pendingApproval.proposedAt,
    reason,
  };
}

test("1. Reject with a reason → the approval in /api/state is rejected and carries that exact reason string", async () => {
  await withServer(4741, async ({ base }) => {
    const cases = [
      { eventId: "reject-with-reason", reason: "no longer needed" },
      { eventId: "reject-with-empty-reason", reason: "" },
    ];

    for (const item of cases) {
      const pending = await createApproval(base, item.eventId);
      const response = await post(base, `/api/approvals/${pending.id}/reject`, {
        reason: item.reason,
      });

      assert.strictEqual(response.status, 200);
      assert.deepStrictEqual(await response.json(), {
        ok: true,
        status: "rejected",
        reason: item.reason,
      });
      const approval = (await getState(base)).approvals.find(entry => entry.id === pending.id);
      assert.deepStrictEqual(approval, expectedApproval(pending, item.reason));
    }
  });
});

test("2. Reject without a body or without a reason → the approval is rejected with the frozen no-reason representation", async () => {
  await withServer(4742, async ({ base }) => {
    const cases = [
      { eventId: "reject-no-body", omitBody: true },
      { eventId: "reject-empty-object", body: {} },
      { eventId: "reject-null-reason", body: { reason: null } },
      { eventId: "reject-number-reason", body: { reason: 7 } },
      { eventId: "reject-object-reason", body: { reason: { text: "no" } } },
      { eventId: "reject-array-reason", body: { reason: ["no"] } },
      { eventId: "reject-boolean-reason", body: { reason: false } },
    ];

    for (const item of cases) {
      const pending = await createApproval(base, item.eventId);
      const response = await post(
        base,
        `/api/approvals/${pending.id}/reject`,
        item.body,
        { omitBody: item.omitBody },
      );

      assert.strictEqual(response.status, 200);
      assert.deepStrictEqual(await response.json(), {
        ok: true,
        status: "rejected",
        reason: null,
      });
      const approval = (await getState(base)).approvals.find(entry => entry.id === pending.id);
      assert.deepStrictEqual(approval, expectedApproval(pending, null));
    }
  });
});

test("3. Rejecting an already-rejected (or otherwise non-pending) approval → 409 and the record is unchanged, including its original reason", async () => {
  await withServer(4743, async ({ base }) => {
    const cases = [
      {
        eventId: "reject-twice",
        makeNonPending: async pending => {
          const response = await post(base, `/api/approvals/${pending.id}/reject`, {
            reason: "keep this reason",
          });
          assert.strictEqual(response.status, 200);
        },
        status: "rejected",
        expectedReason: "keep this reason",
      },
      {
        eventId: "reject-sent",
        makeNonPending: async pending => {
          const response = await post(base, `/api/approvals/${pending.id}/approve`, {});
          assert.strictEqual(response.status, 200);
        },
        status: "sent",
      },
      {
        eventId: "reject-blocked",
        approvalPayload: { to: "stranger@example.com" },
        makeNonPending: async pending => {
          const response = await post(base, `/api/approvals/${pending.id}/approve`, {});
          assert.strictEqual(response.status, 403);
        },
        status: "blocked_recipient",
      },
    ];

    for (const item of cases) {
      const pending = await createApproval(base, item.eventId, item.approvalPayload);
      await item.makeNonPending(pending);
      const beforeReject = (await getState(base)).approvals.find(entry => entry.id === pending.id);

      const response = await post(base, `/api/approvals/${pending.id}/reject`, {
        reason: "replacement reason",
      });
      assert.strictEqual(response.status, 409);
      assert.deepStrictEqual(await response.json(), {
        ok: false,
        error: `already ${item.status}`,
      });

      const afterReject = (await getState(base)).approvals.find(entry => entry.id === pending.id);
      assert.strictEqual(JSON.stringify(afterReject), JSON.stringify(beforeReject));
      if (item.status === "rejected") {
        assert.deepStrictEqual(afterReject, expectedApproval(pending, item.expectedReason));
      } else {
        assert.strictEqual(Object.hasOwn(afterReject, "reason"), false);
      }
    }
  });
});

test("4. Reason survives a server kill and restart", async () => {
  const port = 4744;
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-reject-reason-restart-test-"));
  let proc = startServer(port, dataDir);

  try {
    await waitForHealth(base);
    const pending = await createApproval(base, "reject-restart");
    const response = await post(base, `/api/approvals/${pending.id}/reject`, {
      reason: "persist after SIGKILL",
    });
    assert.strictEqual(response.status, 200);
    await response.arrayBuffer();

    await stopServer(proc);
    proc = startServer(port, dataDir);
    await waitForHealth(base);

    const approval = (await getState(base)).approvals.find(item => item.id === pending.id);
    assert.deepStrictEqual(approval, expectedApproval(pending, "persist after SIGKILL"));
  } finally {
    await stopServer(proc);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
});
