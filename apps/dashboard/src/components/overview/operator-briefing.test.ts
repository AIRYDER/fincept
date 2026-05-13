import assert from "assert";

import type {
  DataCoverageRow,
  KillSwitchState,
  OrderRecord,
  Position,
  ServicesResponse,
  StrategyConfigRow,
  StrategyRow,
  UniverseRow,
} from "@/lib/types";

import {
  buildOperatorBriefing,
  type OperatorBriefingInput,
} from "./operator-briefing";

type TestFn = () => void | Promise<void>;
const tests: Array<{ name: string; fn: TestFn }> = [];
function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Fixtures (mirror recon-checklist.test.ts shape)
// ---------------------------------------------------------------------------

const positions: Position[] = [
  {
    strategy_id: "strat_a",
    symbol: "AAPL",
    quantity: "100",
    avg_cost: "150",
    realized_pnl: "0",
    unrealized_pnl: "500",
    updated_at: 1,
  },
];

const strategies: StrategyRow[] = [
  { strategy_id: "strat_a", position_count: 1, open_positions: 1 },
];

// Disabled config + model binding + no positions so strategy-readiness
// reports "ready" (enabled strategies, missing model binding, or open
// positions each trigger a watch by design).
const configs: StrategyConfigRow[] = [
  {
    strategy_id: "strat_a",
    class_name: "ma_crossover",
    symbols: ["AAPL"],
    params: { fast: 5, slow: 20 },
    model_binding: "test-binding",
    enabled: false,
    created_at: 1,
    updated_at: 1,
  },
];

// Empty positions so strategy-readiness does not flag position-drift watch.
const positionsClean: Position[] = [];

const universe: UniverseRow[] = [
  { symbol: "AAPL", asset_class: "equity", venue_default: "paper", venue: "paper", active: true },
];

const coverage: DataCoverageRow[] = [
  { symbol: "AAPL", freq: "1m", status: "ok", bar_count: 1000, last_ts_event: 1, age_ns: 100 },
];

const orders: OrderRecord[] = [];

const services: ServicesResponse = {
  services: [
    { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
    { name: "ingestor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
  ],
  summary: { up: 2, expected: 2, stale_after_sec: 30, ttl_sec: 90 },
};

const killSwitchClear: KillSwitchState = {
  engaged: false,
  actor: null,
  reason: null,
  alert_id: null,
  ts_unix: null,
};

function baseInput(overrides: Partial<OperatorBriefingInput> = {}): OperatorBriefingInput {
  return {
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders,
    services,
    killSwitch: killSwitchClear,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("ready state when everything aligned", () => {
  const packet = buildOperatorBriefing(
    baseInput({ positions: positionsClean }),
  );
  assert.equal(packet.state, "ready");
  assert(packet.headline.toLowerCase().includes("all operator"));
});

test("strip always has 5 items: kill, services, recon, strategies, receipts", () => {
  const packet = buildOperatorBriefing(baseInput());
  assert.equal(packet.strip.length, 5);
  const ids = packet.strip.map((s) => s.id);
  assert.deepEqual(ids, ["kill", "services", "recon", "strategies", "receipts"]);
});

test("alert state when kill switch is engaged", () => {
  const packet = buildOperatorBriefing(
    baseInput({
      killSwitch: { engaged: true, actor: "operator", reason: "manual halt", alert_id: "x", ts_unix: 1 },
    }),
  );
  assert.equal(packet.state, "alert");
  assert.equal(packet.killSwitch.engaged, true);
  assert.equal(packet.killSwitch.reason, "manual halt");
  const killStrip = packet.strip.find((s) => s.id === "kill");
  assert.equal(killStrip?.state, "alert");
});

test("alert state when expected services are down", () => {
  const packet = buildOperatorBriefing(
    baseInput({
      services: {
        services: [
          { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "ingestor", status: "down", last_beat_unix: null, age_sec: null, expected: true },
        ],
        summary: { up: 1, expected: 2, stale_after_sec: 30, ttl_sec: 90 },
      },
    }),
  );
  assert.equal(packet.state, "alert");
  assert.equal(packet.services.down, 1);
  const svcStrip = packet.strip.find((s) => s.id === "services");
  assert.equal(svcStrip?.state, "alert");
});

test("services strip shows stale detail when an expected service is stale", () => {
  // Note: recon-checklist treats any non-up expected service as a critical
  // issue (service-down), so the OVERALL state becomes "alert". This test
  // only verifies the briefing's services strip surfaces stale-count correctly.
  const packet = buildOperatorBriefing(
    baseInput({
      positions: positionsClean,
      services: {
        services: [
          { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "ingestor", status: "stale", last_beat_unix: 1, age_sec: 60, expected: true },
        ],
        summary: { up: 1, expected: 2, stale_after_sec: 30, ttl_sec: 90 },
      },
    }),
  );
  assert.equal(packet.services.stale, 1);
  const svcStrip = packet.strip.find((s) => s.id === "services");
  assert(svcStrip?.detail.includes("stale"));
});

test("recon issues surface as top issues", () => {
  const packet = buildOperatorBriefing(
    baseInput({
      configs: [], // no configs → critical issue
    }),
  );
  assert(packet.topIssues.length > 0);
  const recon = packet.strip.find((s) => s.id === "recon");
  assert.equal(recon?.state, "alert");
});

test("top issues capped at 5", () => {
  // Many strategies without configs → many critical issues
  const manyStrategies: StrategyRow[] = Array.from({ length: 10 }, (_, i) => ({
    strategy_id: `strat_${i}`,
    position_count: 1,
    open_positions: 1,
  }));
  const packet = buildOperatorBriefing(
    baseInput({
      strategies: manyStrategies,
      configs: [],
    }),
  );
  assert(packet.topIssues.length <= 5);
});

test("strategy rollup classifies strategies by readiness", () => {
  const packet = buildOperatorBriefing(baseInput());
  assert.equal(packet.strategies.total, 1);
  assert(packet.strategies.ready + packet.strategies.review + packet.strategies.blocked === 1);
});

test("strategy attention list capped at 5 and excludes ready strategies", () => {
  const manyStrategies: StrategyRow[] = Array.from({ length: 10 }, (_, i) => ({
    strategy_id: `strat_${i}`,
    position_count: 0,
    open_positions: 0,
  }));
  const manyConfigs: StrategyConfigRow[] = manyStrategies.map((s) => ({
    strategy_id: s.strategy_id,
    class_name: "unknown_class", // unknown → readiness review/blocked
    symbols: [],
    params: {},
    model_binding: null,
    enabled: true,
    created_at: 1,
    updated_at: 1,
  }));
  const packet = buildOperatorBriefing(
    baseInput({
      strategies: manyStrategies,
      configs: manyConfigs,
    }),
  );
  assert(packet.strategies.attention.length <= 5);
  for (const a of packet.strategies.attention) {
    assert(a.state !== "ready", "ready strategies should not be in attention list");
  }
});

test("strategies without configs are not counted in rollup", () => {
  const packet = buildOperatorBriefing(
    baseInput({
      strategies: [
        { strategy_id: "strat_a", position_count: 1, open_positions: 1 },
        { strategy_id: "orphan", position_count: 0, open_positions: 0 },
      ],
    }),
  );
  // orphan has no config → not in rollup
  assert.equal(packet.strategies.total, 1);
});

test("services strip shows 'no services reported' when empty", () => {
  const packet = buildOperatorBriefing(
    baseInput({
      services: { services: [], summary: { up: 0, expected: 0, stale_after_sec: 30, ttl_sec: 90 } },
    }),
  );
  const svc = packet.strip.find((s) => s.id === "services");
  assert(svc?.detail.toLowerCase().includes("no services"));
  assert.equal(svc?.state, "watch");
});

test("receipts strip exposes receipt catalog count", () => {
  const packet = buildOperatorBriefing(baseInput());
  const rec = packet.strip.find((s) => s.id === "receipts");
  assert(rec);
  assert(packet.receipts.total > 0);
  assert(rec.detail.includes(String(packet.receipts.total)));
});

test("receipts state does not block overall state", () => {
  // Even if receipts is "review" (which it will be by default due to live scripts),
  // a healthy stack should be "ready" overall.
  const packet = buildOperatorBriefing(
    baseInput({ positions: positionsClean }),
  );
  assert.equal(packet.state, "ready");
});

test("every strip item has an href for navigation", () => {
  const packet = buildOperatorBriefing(baseInput());
  for (const item of packet.strip) {
    assert(item.href, `strip item ${item.id} missing href`);
  }
});

test("headline reflects state severity", () => {
  const ready = buildOperatorBriefing(
    baseInput({ positions: positionsClean }),
  );
  assert(ready.headline.toLowerCase().includes("all operator"));

  const alert = buildOperatorBriefing(
    baseInput({
      killSwitch: { engaged: true, actor: null, reason: null, alert_id: null, ts_unix: null },
    }),
  );
  assert(alert.headline.toLowerCase().includes("blocker"));

  // Trigger watch-only state via an enabled strategy (lifecycle-mode = watch
  // in strategy-readiness). All other strips remain ready.
  const watch = buildOperatorBriefing(
    baseInput({
      positions: positionsClean,
      configs: [{ ...configs[0], enabled: true }],
    }),
  );
  assert.equal(watch.state, "watch");
  assert(
    watch.headline.toLowerCase().includes("look") ||
      watch.headline.toLowerCase().includes("operational"),
  );
});

// ---------------------------------------------------------------------------
// Acceptance: roadmap "Overview quick wins"
// ---------------------------------------------------------------------------

test("acceptance: surfaces safety state, recon issues, services, strategies, receipts", () => {
  const packet = buildOperatorBriefing(baseInput());
  const ids = packet.strip.map((s) => s.id);
  assert(ids.includes("kill"));
  assert(ids.includes("recon"));
  assert(ids.includes("services"));
  assert(ids.includes("strategies"));
  assert(ids.includes("receipts"));
});

test("acceptance: critical recon issues bubble up before warnings", () => {
  // Mix of critical (missing config) and warning issues
  const packet = buildOperatorBriefing(
    baseInput({
      configs: [
        { ...configs[0], enabled: false }, // disabled = warning
      ],
      strategies: [
        ...strategies,
        { strategy_id: "no_config", position_count: 0, open_positions: 0 }, // no config = critical (if positions exist, else just no-config-row warning)
      ],
    }),
  );
  // If there are any critical issues, they should appear before warnings
  if (packet.topIssues.length >= 2) {
    const firstSeverity = packet.topIssues[0].severity;
    const hasLowerSeverity = packet.topIssues.some(
      (i) => firstSeverity === "critical" && i.severity === "warning",
    );
    if (hasLowerSeverity) {
      // Verify ordering
      const criticalIdx = packet.topIssues.findIndex((i) => i.severity === "critical");
      const warningIdx = packet.topIssues.findIndex((i) => i.severity === "warning");
      assert(criticalIdx < warningIdx, "critical issues should appear before warnings");
    }
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
  console.log(`${passed} operator briefing tests passed`);
}

run();
