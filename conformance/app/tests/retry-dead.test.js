const { test } = require("node:test");
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const SERVER = path.join(__dirname, "..", "server.js");

function startServer(port, dataDir, workerMs = 50) {
  return spawn(process.execPath, [SERVER], {
    env: {
      ...process.env,
      PORT: String(port),
      DATA_DIR: dataDir,
      WORKER_MS: String(workerMs),
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

async function withServer(port, run, workerMs = 50) {
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-retry-dead-test-"));
  const proc = startServer(port, dataDir, workerMs);

  try {
    await waitForHealth(base);
    await run({ base, dataDir, proc });
  } finally {
    await stopServer(proc);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
}

async function postEvent(base, id, payload = {}) {
  const response = await fetch(`${base}/webhook/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, payload }),
  });
  assert.strictEqual(response.status, 200);
  return response.json();
}

function retryJob(base, jobId) {
  return fetch(`${base}/api/jobs/${jobId}/retry`, { method: "POST" });
}

async function getState(base) {
  return (await fetch(`${base}/api/state`)).json();
}

async function waitForJob(base, jobId, predicate, tries = 100) {
  for (let i = 0; i < tries; i++) {
    const state = await getState(base);
    const job = state.jobs.find(item => item.id === jobId);
    if (job && predicate(job, state)) return { job, state };
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`job ${jobId} did not reach the expected state`);
}

test("1. A dead job, retried → pending again; a job whose cause is fixed completes and produces its proposed action as normal", async () => {
  const port = 4751;
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-retry-transition-test-"));
  let proc = startServer(port, dataDir, 50);

  try {
    await waitForHealth(base);
    const eventId = "retry-then-complete";
    const jobId = `job-${eventId}`;
    await postEvent(base, eventId, {
      failTimes: 3,
      to: "test@example.com",
      subject: "Recovered action",
      body: "The retry completed.",
    });
    const { job: deadJob } = await waitForJob(base, jobId, job => job.status === "dead");
    assert.deepStrictEqual(
      {
        attempts: deadJob.attempts,
        lastError: deadJob.lastError,
        retries: deadJob.retries,
        attemptsSinceRetry: deadJob.attemptsSinceRetry,
      },
      {
        attempts: 3,
        lastError: "simulated failure on attempt 3",
        retries: 0,
        attemptsSinceRetry: 3,
      },
    );

    await stopServer(proc);
    proc = startServer(port, dataDir, 60_000);
    await waitForHealth(base);

    const response = await retryJob(base, jobId);
    assert.strictEqual(response.status, 200);
    assert.deepStrictEqual(await response.json(), {
      ok: true,
      status: "pending",
      retries: 1,
    });

    await stopServer(proc);
    proc = startServer(port, dataDir, 60_000);
    await waitForHealth(base);
    const persistedRetry = (await getState(base)).jobs.find(item => item.id === jobId);
    assert.deepStrictEqual(
      {
        status: persistedRetry.status,
        attempts: persistedRetry.attempts,
        lastError: persistedRetry.lastError,
        retries: persistedRetry.retries,
        attemptsSinceRetry: persistedRetry.attemptsSinceRetry,
      },
      {
        status: "pending",
        attempts: 3,
        lastError: "simulated failure on attempt 3",
        retries: 1,
        attemptsSinceRetry: 0,
      },
    );

    await stopServer(proc);
    proc = startServer(port, dataDir, 50);
    await waitForHealth(base);
    const { job, state } = await waitForJob(base, jobId, item => item.status === "done");
    assert.deepStrictEqual(
      {
        status: job.status,
        attempts: job.attempts,
        lastError: job.lastError,
        retries: job.retries,
        attemptsSinceRetry: job.attemptsSinceRetry,
      },
      {
        status: "done",
        attempts: 4,
        lastError: null,
        retries: 1,
        attemptsSinceRetry: 1,
      },
    );
    assert.deepStrictEqual(
      state.approvals.find(item => item.jobId === jobId)?.action,
      {
        to: "test@example.com",
        subject: "Recovered action",
        body: "The retry completed.",
      },
    );
  } finally {
    await stopServer(proc);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
});

test("2. A dead job, retried, still failing → dead again after exactly 3 new attempts, with cumulative attempts visible on the record", async () => {
  const port = 4752;
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-second-retry-test-"));
  let proc = startServer(port, dataDir, 50);

  try {
    await waitForHealth(base);
    const eventId = "retry-still-failing";
    const jobId = `job-${eventId}`;
    await postEvent(base, eventId, { failTimes: 99 });
    await waitForJob(base, jobId, job => job.status === "dead");

    const response = await retryJob(base, jobId);
    assert.strictEqual(response.status, 200);
    assert.deepStrictEqual(await response.json(), {
      ok: true,
      status: "pending",
      retries: 1,
    });

    const { job } = await waitForJob(
      base,
      jobId,
      item => item.status === "dead" && item.retries === 1,
    );
    assert.deepStrictEqual(
      {
        status: job.status,
        attempts: job.attempts,
        lastError: job.lastError,
        retries: job.retries,
        attemptsSinceRetry: job.attemptsSinceRetry,
      },
      {
        status: "dead",
        attempts: 6,
        lastError: "simulated failure on attempt 6",
        retries: 1,
        attemptsSinceRetry: 3,
      },
    );

    await stopServer(proc);
    proc = startServer(port, dataDir, 60_000);
    await waitForHealth(base);
    const secondResponse = await retryJob(base, jobId);
    assert.strictEqual(secondResponse.status, 200);
    assert.deepStrictEqual(await secondResponse.json(), {
      ok: true,
      status: "pending",
      retries: 2,
    });

    const secondRetry = (await getState(base)).jobs.find(item => item.id === jobId);
    assert.deepStrictEqual(
      {
        status: secondRetry.status,
        attempts: secondRetry.attempts,
        lastError: secondRetry.lastError,
        retries: secondRetry.retries,
        attemptsSinceRetry: secondRetry.attemptsSinceRetry,
      },
      {
        status: "pending",
        attempts: 6,
        lastError: "simulated failure on attempt 6",
        retries: 2,
        attemptsSinceRetry: 0,
      },
    );

    await stopServer(proc);
    proc = startServer(port, dataDir, 50);
    await waitForHealth(base);
    const { job: twiceDeadJob } = await waitForJob(
      base,
      jobId,
      item => item.status === "dead" && item.retries === 2,
    );
    assert.deepStrictEqual(
      {
        status: twiceDeadJob.status,
        attempts: twiceDeadJob.attempts,
        lastError: twiceDeadJob.lastError,
        retries: twiceDeadJob.retries,
        attemptsSinceRetry: twiceDeadJob.attemptsSinceRetry,
      },
      {
        status: "dead",
        attempts: 9,
        lastError: "simulated failure on attempt 9",
        retries: 2,
        attemptsSinceRetry: 3,
      },
    );
  } finally {
    await stopServer(proc);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
});

test("3. Retry of a non-dead job (pending or done) → refused with 409, record unchanged", async () => {
  await withServer(4753, async ({ base }) => {
    const eventId = "retry-pending";
    const jobId = `job-${eventId}`;
    await postEvent(base, eventId);
    const before = (await getState(base)).jobs.find(item => item.id === jobId);

    const response = await retryJob(base, jobId);
    assert.strictEqual(response.status, 409);
    assert.deepStrictEqual(await response.json(), {
      ok: false,
      error: "only dead jobs can be retried (status: pending)",
    });
    const after = (await getState(base)).jobs.find(item => item.id === jobId);
    assert.strictEqual(JSON.stringify(after), JSON.stringify(before));
    assert.strictEqual(after.retries, 0);
    assert.strictEqual(after.attemptsSinceRetry, 0);
  }, 60_000);

  await withServer(4754, async ({ base }) => {
    const eventId = "retry-done";
    const jobId = `job-${eventId}`;
    await postEvent(base, eventId);
    const { job: before } = await waitForJob(base, jobId, job => job.status === "done");

    const response = await retryJob(base, jobId);
    assert.strictEqual(response.status, 409);
    assert.deepStrictEqual(await response.json(), {
      ok: false,
      error: "only dead jobs can be retried (status: done)",
    });
    const after = (await getState(base)).jobs.find(item => item.id === jobId);
    assert.strictEqual(JSON.stringify(after), JSON.stringify(before));
    assert.strictEqual(after.retries, 0);
    assert.strictEqual(after.attemptsSinceRetry, 1);
  });
});

test("4. Kill the server right after a retry is accepted; on restart the re-queued job is processed", async () => {
  const port = 4755;
  const base = `http://localhost:${port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-retry-dead-restart-test-"));
  let proc = startServer(port, dataDir, 1_000);

  try {
    await waitForHealth(base);
    const eventId = "retry-crash-recovery";
    const jobId = `job-${eventId}`;
    await postEvent(base, eventId, { failTimes: 3, subject: "Recovered after restart" });
    await waitForJob(base, jobId, job => job.status === "dead", 50);

    const response = await retryJob(base, jobId);
    assert.strictEqual(response.status, 200);
    assert.deepStrictEqual(await response.json(), {
      ok: true,
      status: "pending",
      retries: 1,
    });
    await stopServer(proc);

    proc = startServer(port, dataDir, 50);
    await waitForHealth(base);
    const { job, state } = await waitForJob(base, jobId, item => item.status === "done");
    assert.deepStrictEqual(
      {
        attempts: job.attempts,
        retries: job.retries,
        attemptsSinceRetry: job.attemptsSinceRetry,
      },
      { attempts: 4, retries: 1, attemptsSinceRetry: 1 },
    );
    assert.strictEqual(
      state.approvals.some(item => item.jobId === jobId && item.action.subject === "Recovered after restart"),
      true,
    );
  } finally {
    await stopServer(proc);
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
});

test("Unknown job → 404 with exactly the frozen no-such-job response", async () => {
  await withServer(4756, async ({ base }) => {
    const response = await retryJob(base, "job-unknown");
    assert.strictEqual(response.status, 404);
    assert.deepStrictEqual(await response.json(), {
      ok: false,
      error: "no such job",
    });
  });
});
