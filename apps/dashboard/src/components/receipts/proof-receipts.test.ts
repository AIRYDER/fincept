import assert from "assert";

import {
  buildProofReceiptCenter,
  PROOF_RECEIPTS,
  type ProofReceiptDefinition,
} from "./proof-receipts";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const baseReceipt: ProofReceiptDefinition = {
  id: "demo",
  title: "Demo receipt",
  description: "Demo proof receipt.",
  channel: "dashboard_export",
  runtime: "browser",
  producer: "DemoPanel",
  route: "/demo",
  schema: "demo.v1",
  scope: ["demo"],
  liveDependencies: [],
};

test("builds the default proof receipt center catalog", () => {
  const center = buildProofReceiptCenter();
  assert.equal(center.stats.total, PROOF_RECEIPTS.length);
  assert.equal(center.state, "review");
  assert(center.checks.some((check) => check.id === "live-boundary" && check.severity === "watch"));
  assert(center.receipts.some((receipt) => receipt.id === "paper-spine-replay"));
});

test("reports ready when only offline dashboard receipts are cataloged", () => {
  const center = buildProofReceiptCenter([baseReceipt]);
  assert.equal(center.state, "ready");
  assert.equal(center.stats.dashboardExports, 1);
  assert.equal(center.stats.liveScripts, 0);
});

test("blocks an empty receipt catalog", () => {
  const center = buildProofReceiptCenter([]);
  assert.equal(center.state, "blocked");
  assert(center.checks.some((check) => check.id === "catalog" && check.severity === "fail"));
});

test("blocks receipts missing schema metadata", () => {
  const center = buildProofReceiptCenter([{ ...baseReceipt, schema: "" }]);
  assert.equal(center.state, "blocked");
  assert(center.checks.some((check) => check.id === "schemas" && check.severity === "fail"));
});

async function run() {
  let passed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      passed += 1;
      console.log(`ok - ${name}`);
    } catch (error) {
      console.error(`not ok - ${name}`);
      console.error(error);
      process.exitCode = 1;
      return;
    }
  }
  console.log(`${passed} proof receipt tests passed`);
}

void run();
