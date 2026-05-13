import assert from "assert";

import {
  freshnessIntent,
  healthIntent,
  pnlIntent,
  severityIntent,
  sourceIntent,
  INTENT_BADGE_VARIANT,
  INTENT_BG,
  INTENT_BORDER,
  INTENT_DOT,
  INTENT_TEXT,
  type SemanticIntent,
} from "@/lib/design-tokens";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

const ALL_INTENTS: SemanticIntent[] = ["verified", "degraded", "critical", "ai", "healthy", "inactive"];

test("every semantic intent has a text class", () => {
  for (const intent of ALL_INTENTS) {
    assert(INTENT_TEXT[intent], `missing INTENT_TEXT for ${intent}`);
  }
});

test("every semantic intent has a bg class", () => {
  for (const intent of ALL_INTENTS) {
    assert(INTENT_BG[intent], `missing INTENT_BG for ${intent}`);
  }
});

test("every semantic intent has a border class", () => {
  for (const intent of ALL_INTENTS) {
    assert(INTENT_BORDER[intent], `missing INTENT_BORDER for ${intent}`);
  }
});

test("every semantic intent has a dot class", () => {
  for (const intent of ALL_INTENTS) {
    assert(INTENT_DOT[intent], `missing INTENT_DOT for ${intent}`);
  }
});

test("every semantic intent has a badge variant", () => {
  for (const intent of ALL_INTENTS) {
    assert(INTENT_BADGE_VARIANT[intent], `missing INTENT_BADGE_VARIANT for ${intent}`);
  }
});

test("verified uses cyan", () => {
  assert(INTENT_TEXT.verified.includes("cyan"));
  assert(INTENT_DOT.verified.includes("cyan"));
  assert(INTENT_BG.verified.includes("cyan"));
});

test("degraded uses amber", () => {
  assert(INTENT_TEXT.degraded.includes("amber"));
  assert(INTENT_DOT.degraded.includes("amber"));
});

test("critical uses red/short", () => {
  assert(INTENT_TEXT.critical.includes("short"));
  assert(INTENT_DOT.critical.includes("short"));
});

test("ai uses purple", () => {
  assert(INTENT_TEXT.ai.includes("purple"));
  assert(INTENT_DOT.ai.includes("purple"));
});

test("healthy uses green/long", () => {
  assert(INTENT_TEXT.healthy.includes("long"));
  assert(INTENT_DOT.healthy.includes("long"));
});

test("inactive uses muted/gray", () => {
  assert(INTENT_TEXT.inactive.includes("muted"));
  assert(INTENT_DOT.inactive.includes("muted"));
});

test("healthIntent: ok → verified", () => {
  assert.equal(healthIntent(true), "verified");
});

test("healthIntent: ok but stale → degraded", () => {
  assert.equal(healthIntent(true, true), "degraded");
});

test("healthIntent: not ok → critical", () => {
  assert.equal(healthIntent(false), "critical");
});

test("pnlIntent: positive → healthy", () => {
  assert.equal(pnlIntent(100), "healthy");
});

test("pnlIntent: negative → critical", () => {
  assert.equal(pnlIntent(-50), "critical");
});

test("pnlIntent: zero → inactive", () => {
  assert.equal(pnlIntent(0), "inactive");
});

test("sourceIntent: system → verified", () => {
  assert.equal(sourceIntent("system"), "verified");
});

test("sourceIntent: model → ai", () => {
  assert.equal(sourceIntent("model"), "ai");
});

test("sourceIntent: human → healthy", () => {
  assert.equal(sourceIntent("human"), "healthy");
});

test("sourceIntent: unknown → inactive", () => {
  assert.equal(sourceIntent("unknown"), "inactive");
});

test("freshnessIntent: fresh → verified", () => {
  assert.equal(freshnessIntent(10), "verified");
});

test("freshnessIntent: stale → degraded", () => {
  assert.equal(freshnessIntent(600), "degraded");
});

test("freshnessIntent: dead → critical", () => {
  assert.equal(freshnessIntent(5000), "critical");
});

test("freshnessIntent: respects custom thresholds", () => {
  assert.equal(freshnessIntent(10, 5, 60), "degraded");
  assert.equal(freshnessIntent(100, 5, 60), "critical");
});

test("severityIntent: ok → healthy", () => {
  assert.equal(severityIntent("ok"), "healthy");
});

test("severityIntent: info → verified", () => {
  assert.equal(severityIntent("info"), "verified");
});

test("severityIntent: warning → degraded", () => {
  assert.equal(severityIntent("warning"), "degraded");
});

test("severityIntent: critical → critical", () => {
  assert.equal(severityIntent("critical"), "critical");
});

test("AI output is visually distinct from verified system state", () => {
  // Purple vs cyan — must be different classes
  assert.notEqual(INTENT_TEXT.ai, INTENT_TEXT.verified);
  assert.notEqual(INTENT_DOT.ai, INTENT_DOT.verified);
  assert.notEqual(INTENT_BG.ai, INTENT_BG.verified);
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
  console.log(`${passed} design token tests passed`);
}

run();
