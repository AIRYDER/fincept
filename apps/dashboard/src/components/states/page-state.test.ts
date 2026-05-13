import assert from "assert";

import {
  authState,
  demoState,
  emptyState,
  fatalState,
  loadingState,
  okState,
  partialState,
  providerState,
  queryToPageState,
  staleState,
  type PageState,
  type PageStateType,
} from "./page-state";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Builder tests
// ---------------------------------------------------------------------------

test("emptyState has type empty and remediation", () => {
  const s = emptyState();
  assert.equal(s.type, "empty");
  assert(s.remediation);
});

test("loadingState has type loading and no remediation", () => {
  const s = loadingState();
  assert.equal(s.type, "loading");
  assert.equal(s.remediation, null);
});

test("authState has type auth and sign-in remediation", () => {
  const s = authState();
  assert.equal(s.type, "auth");
  assert(s.remediation?.includes("Sign in"));
});

test("providerState includes provider name and remediation", () => {
  const s = providerState("OpenBB", "timeout");
  assert.equal(s.type, "provider");
  assert.equal(s.provider, "OpenBB");
  assert(s.remediation?.includes("OpenBB"));
  assert.equal(s.errorDetail, "timeout");
});

test("staleState includes age and timestamp", () => {
  const s = staleState(1_700_000_000, 600);
  assert.equal(s.type, "stale");
  assert(s.description.includes("600s old"));
  assert(s.lastOkAt);
  assert(s.remediation);
});

test("partialState lists available and missing parts", () => {
  const s = partialState(["positions", "orders"], ["coverage"]);
  assert.equal(s.type, "partial");
  assert.deepEqual(s.missingParts, ["coverage"]);
  assert(s.description.includes("positions"));
  assert(s.remediation?.includes("coverage"));
});

test("fatalState has type fatal and error detail", () => {
  const s = fatalState("Connection refused");
  assert.equal(s.type, "fatal");
  assert.equal(s.errorDetail, "Connection refused");
  assert(s.remediation?.includes("API"));
});

test("demoState has type demo and is always labeled", () => {
  const s = demoState("OpenBB");
  assert.equal(s.type, "demo");
  assert(s.description.includes("sample data"));
  assert(s.description.includes("OpenBB"));
  assert(s.remediation?.includes("real data"));
});

test("okState has type ok", () => {
  const s = okState();
  assert.equal(s.type, "ok");
  assert.equal(s.remediation, null);
});

// ---------------------------------------------------------------------------
// queryToPageState tests
// ---------------------------------------------------------------------------

test("queryToPageState: no auth → auth state", () => {
  const s = queryToPageState({ isLoading: false, isError: false, data: [], noAuth: true });
  assert.equal(s.type, "auth");
});

test("queryToPageState: loading with no data → loading state", () => {
  const s = queryToPageState({ isLoading: true, isError: false, data: undefined });
  assert.equal(s.type, "loading");
});

test("queryToPageState: error with provider → provider state", () => {
  const s = queryToPageState({
    isLoading: false,
    isError: true,
    error: new Error("timeout"),
    data: undefined,
    provider: "OpenBB",
  });
  assert.equal(s.type, "provider");
  assert.equal(s.provider, "OpenBB");
});

test("queryToPageState: error without provider → fatal state", () => {
  const s = queryToPageState({
    isLoading: false,
    isError: true,
    error: new Error("Connection refused"),
    data: undefined,
  });
  assert.equal(s.type, "fatal");
  assert(s.errorDetail?.includes("Connection refused"));
});

test("queryToPageState: empty array → empty state", () => {
  const s = queryToPageState({ isLoading: false, isError: false, data: [] });
  assert.equal(s.type, "empty");
});

test("queryToPageState: null data → empty state", () => {
  const s = queryToPageState({ isLoading: false, isError: false, data: null });
  assert.equal(s.type, "empty");
});

test("queryToPageState: isDemo flag → demo state", () => {
  const s = queryToPageState({ isLoading: false, isError: false, data: [1, 2, 3], isDemo: true });
  assert.equal(s.type, "demo");
});

test("queryToPageState: stale data → stale state", () => {
  const s = queryToPageState({
    isLoading: false,
    isError: false,
    data: [1, 2, 3],
    dataAgeSec: 600,
    staleAfterSec: 300,
  });
  assert.equal(s.type, "stale");
});

test("queryToPageState: fresh data → ok state", () => {
  const s = queryToPageState({
    isLoading: false,
    isError: false,
    data: [1, 2, 3],
    dataAgeSec: 10,
    staleAfterSec: 300,
  });
  assert.equal(s.type, "ok");
});

test("queryToPageState: partial data → partial state", () => {
  const s = queryToPageState({
    isLoading: false,
    isError: false,
    data: { positions: [1] },
    partial: { available: ["positions"], missing: ["coverage"] },
  });
  assert.equal(s.type, "partial");
  assert.deepEqual(s.missingParts, ["coverage"]);
});

test("queryToPageState: data present, no flags → ok state", () => {
  const s = queryToPageState({ isLoading: false, isError: false, data: { foo: "bar" } });
  assert.equal(s.type, "ok");
});

// ---------------------------------------------------------------------------
// Acceptance criteria tests
// ---------------------------------------------------------------------------

test("acceptance: external provider failures are never shown as generic crashes", () => {
  // Provider errors get their own state type, not fatal
  const s = queryToPageState({
    isLoading: false,
    isError: true,
    error: new Error("OpenBB timeout"),
    data: undefined,
    provider: "OpenBB",
  });
  assert.equal(s.type, "provider");
  assert.notEqual(s.type, "fatal");
});

test("acceptance: demo data is always labeled", () => {
  const s = demoState("OpenBB");
  assert(s.description.toLowerCase().includes("sample"));
  assert(s.description.toLowerCase().includes("not live"));
});

test("acceptance: stale data includes last timestamp and likely remediation", () => {
  const s = staleState(1_700_000_000, 600);
  assert(s.lastOkAt !== null);
  assert(s.remediation !== null);
  assert(s.description.includes("old"));
});

test("acceptance: pages with partial data still render usable summaries", () => {
  const s = partialState(["positions", "orders"], ["coverage"]);
  // Partial state describes what IS available, not just what's missing
  assert(s.description.includes("positions"));
  assert(s.description.includes("orders"));
  assert(s.missingParts?.includes("coverage"));
});

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

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
    }
  }
  console.log(`${passed} page state tests passed`);
}

run();
