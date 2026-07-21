#!/usr/bin/env node
// Convert one controller-supplied, bounded patch result into a canonical artifact.

import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";

const INPUT_SCHEMA = "nysa.software-factory.provider-worker-input/v1";
const ARTIFACT_SCHEMA = "nysa.software-factory.provider-patch-artifact/v1";

function canonical(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonical(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function exactKeys(value, expected) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

const input = JSON.parse(await readFile("/workspace/payload/input", "utf8"));
const identity = JSON.parse(
  await readFile("/workspace/payload/identity.json", "utf8"),
);
if (!exactKeys(input, ["files", "patch", "schema", "telemetry"])
    || input.schema !== INPUT_SCHEMA
    || typeof input.patch !== "string"
    || !Array.isArray(input.files)
    || input.files.length === 0
    || input.files.some((item) => typeof item !== "string")
    || [...new Set(input.files)].sort().join("\0") !== input.files.join("\0")) {
  throw new Error("worker input is malformed");
}
if (!exactKeys(
  input.telemetry,
  [
    "charge_micro_usd", "duration_ms", "input_tokens", "output_tokens",
    "provider_request_id",
  ],
)) {
  throw new Error("worker telemetry is malformed");
}
for (const field of [
  "charge_micro_usd", "duration_ms", "input_tokens", "output_tokens",
]) {
  if (!Number.isSafeInteger(input.telemetry[field]) || input.telemetry[field] < 0) {
    throw new Error("worker telemetry is malformed");
  }
}

const patch = Buffer.from(input.patch, "utf8");
const artifact = {
  schema: ARTIFACT_SCHEMA,
  attempt_id: identity.attempt_id,
  base_sha: identity.base_sha,
  binding_sha256: identity.binding_sha256,
  files: input.files,
  image_digest: identity.image_digest,
  input_sha256: identity.input_sha256,
  patch_path: "changes.patch",
  patch_sha256: digest(patch),
  policy_sha256: identity.policy_sha256,
  role: identity.role,
  route_id: identity.route_id,
  source_sha256: identity.source_sha256,
  telemetry: input.telemetry,
  ticket: identity.ticket,
  worker_sha256: identity.worker_sha256,
};
await mkdir("/workspace/artifacts", { recursive: true });
await writeFile("/workspace/artifacts/changes.patch", patch, { mode: 0o600 });
await writeFile(
  "/workspace/artifacts/artifact.json",
  `${canonical(artifact)}\n`,
  { mode: 0o600 },
);
