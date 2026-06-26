import assert from "assert";

import {
  BRAND,
  directionOf,
  formatSignedPct,
  formatSignedUsd,
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
// Brand + signed-value formatting helpers
// ---------------------------------------------------------------------------

test("BRAND exposes a stable name and accent", () => {
  assert.equal(typeof BRAND.name, "string");
  assert.ok(BRAND.name.length > 0, "BRAND.name must not be empty");
  assert.equal(BRAND.accent, "cobalt");
});

test("directionOf: positive → up", () => {
  assert.equal(directionOf(1), "up");
  assert.equal(directionOf(0.0001), "up");
});

test("directionOf: negative → down", () => {
  assert.equal(directionOf(-1), "down");
  assert.equal(directionOf(-0.0001), "down");
});

test("directionOf: exact zero → flat", () => {
  assert.equal(directionOf(0), "flat");
});

test("directionOf: non-finite → flat (never throws)", () => {
  assert.equal(directionOf(Number.NaN), "flat");
  assert.equal(directionOf(Number.POSITIVE_INFINITY), "flat");
  assert.equal(directionOf(Number.NEGATIVE_INFINITY), "flat");
});

test("formatSignedUsd: positive value gets + sign and two decimals", () => {
  assert.equal(formatSignedUsd(1.234), "+$1.23");
  assert.equal(formatSignedUsd(1000), "+$1,000.00");
});

test("formatSignedUsd: negative value gets - sign and absolute magnitude", () => {
  assert.equal(formatSignedUsd(-1.234), "-$1.23");
  assert.equal(formatSignedUsd(-1000), "-$1,000.00");
});

test("formatSignedUsd: zero has no sign (not +$0.00 or -$0.00)", () => {
  assert.equal(formatSignedUsd(0), "$0.00");
});

test("formatSignedUsd: null / undefined / NaN render em dash", () => {
  assert.equal(formatSignedUsd(null), "—");
  assert.equal(formatSignedUsd(undefined), "—");
  assert.equal(formatSignedUsd(Number.NaN), "—");
  assert.equal(formatSignedUsd(Number.POSITIVE_INFINITY), "—");
});

test("formatSignedPct: positive value gets + sign and two decimals", () => {
  assert.equal(formatSignedPct(1.234), "+1.23%");
  assert.equal(formatSignedPct(0.5), "+0.50%");
});

test("formatSignedPct: negative value gets - sign and absolute magnitude", () => {
  assert.equal(formatSignedPct(-1.234), "-1.23%");
  assert.equal(formatSignedPct(-0.5), "-0.50%");
});

test("formatSignedPct: zero has no sign", () => {
  assert.equal(formatSignedPct(0), "0.00%");
});

test("formatSignedPct: null / undefined / NaN render em dash", () => {
  assert.equal(formatSignedPct(null), "—");
  assert.equal(formatSignedPct(undefined), "—");
  assert.equal(formatSignedPct(Number.NaN), "—");
  assert.equal(formatSignedPct(Number.POSITIVE_INFINITY), "—");
});

test("signed formatters agree with directionOf on sign boundary", () => {
  // For any non-zero finite value, the formatter's leading character
  // must match the direction directionOf reports.
  for (const v of [0.01, -0.01, 1234.5, -1234.5]) {
    const formatted = formatSignedUsd(v);
    const dir = directionOf(v);
    if (dir === "up") assert.ok(formatted.startsWith("+"), `expected + for ${v}`);
    else if (dir === "down") assert.ok(formatted.startsWith("-"), `expected - for ${v}`);
  }
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
