import assert from "assert";

import { buildPositionPosture } from "./position-posture";

const nowMs = 1_700_000_100_000;

const base = {
  strategy_id: "strat_a",
  symbol: "AAPL",
  quantity: "10",
  avg_cost: "100",
  realized_pnl: "5",
  unrealized_pnl: "10",
  updated_at: 1_700_000_000,
};

const tests: Array<{ name: string; fn: () => void }> = [];
function test(name: string, fn: () => void) {
  tests.push({ name, fn });
}

test("classifies long side as healthy LONG", () => {
  const posture = buildPositionPosture(base, nowMs);
  assert.equal(posture.side, "long");
  assert.equal(posture.sideLabel, "LONG");
  assert.equal(posture.sideIntent, "healthy");
});

test("classifies short side as critical SHORT", () => {
  const posture = buildPositionPosture({ ...base, quantity: "-3" }, nowMs);
  assert.equal(posture.side, "short");
  assert.equal(posture.sideLabel, "SHORT");
  assert.equal(posture.sideIntent, "critical");
});

test("classifies flat side as inactive FLAT", () => {
  const posture = buildPositionPosture({ ...base, quantity: "0" }, nowMs);
  assert.equal(posture.side, "flat");
  assert.equal(posture.sideLabel, "FLAT");
  assert.equal(posture.sideIntent, "inactive");
});

test("computes freshness age from updated_at seconds", () => {
  const posture = buildPositionPosture(base, nowMs);
  assert.equal(posture.freshnessAgeSec, 100);
});

test("returns null freshness for missing updated_at", () => {
  const posture = buildPositionPosture({ ...base, updated_at: 0 }, nowMs);
  assert.equal(posture.freshnessAgeSec, null);
});

test("computes absolute exposure", () => {
  const posture = buildPositionPosture({ ...base, quantity: "-3", avg_cost: "25" }, nowMs);
  assert.equal(posture.exposure, 75);
});

test("classifies pnl intent from total pnl", () => {
  assert.equal(buildPositionPosture(base, nowMs).pnlIntent, "healthy");
  assert.equal(buildPositionPosture({ ...base, realized_pnl: "-20", unrealized_pnl: "5" }, nowMs).pnlIntent, "critical");
  assert.equal(buildPositionPosture({ ...base, realized_pnl: "-5", unrealized_pnl: "5" }, nowMs).pnlIntent, "inactive");
});

test("identifies mark source", () => {
  assert.equal(buildPositionPosture({ ...base, mark_px: "101" }, nowMs).markSource, "live");
  assert.equal(buildPositionPosture(base, nowMs).markSource, "implied");
  assert.equal(buildPositionPosture({ ...base, quantity: "0" }, nowMs).markSource, "cost");
});

let passed = 0;
for (const { name, fn } of tests) {
  try {
    fn();
    passed += 1;
    console.log(`ok - ${name}`);
  } catch (error) {
    console.error(`not ok - ${name}`);
    console.error(error);
    process.exitCode = 1;
  }
}
console.log(`${passed} position posture tests passed`);
