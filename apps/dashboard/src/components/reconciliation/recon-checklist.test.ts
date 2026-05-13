import assert from "assert";

import type {
  DataCoverageRow,
  OrderRecord,
  Position,
  ServicesResponse,
  StrategyConfigRow,
  StrategyRow,
  UniverseRow,
} from "@/lib/types";

import {
  buildReconChecklist,
  buildReconReceipt,
  reconReceiptFilename,
} from "./recon-checklist";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Fixtures
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
  {
    strategy_id: "strat_a",
    symbol: "NVDA",
    quantity: "50",
    avg_cost: "400",
    realized_pnl: "0",
    unrealized_pnl: "-200",
    updated_at: 1,
  },
];

const strategies: StrategyRow[] = [
  { strategy_id: "strat_a", position_count: 2, open_positions: 2 },
];

const configs: StrategyConfigRow[] = [
  {
    strategy_id: "strat_a",
    class_name: "PositionTracker",
    symbols: ["AAPL", "NVDA"],
    params: {},
    model_binding: null,
    enabled: true,
    created_at: 1,
    updated_at: 1,
  },
];

const universe: UniverseRow[] = [
  { symbol: "AAPL", asset_class: "equity", venue_default: "paper", venue: "paper", active: true },
  { symbol: "NVDA", asset_class: "equity", venue_default: "paper", venue: "paper", active: true },
];

const coverage: DataCoverageRow[] = [
  { symbol: "AAPL", freq: "1m", status: "ok", bar_count: 1000, last_ts_event: 1, age_ns: 100 },
  { symbol: "NVDA", freq: "1m", status: "ok", bar_count: 1000, last_ts_event: 1, age_ns: 100 },
];

const orders: OrderRecord[] = [];

const services: ServicesResponse = {
  services: [
    { name: "gbm_predictor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
  ],
  summary: { up: 1, expected: 1, stale_after_sec: 30, ttl_sec: 90 },
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("builds a clean checklist when everything is aligned", () => {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders,
    services,
  });
  assert.equal(summary.state, "clean");
  assert.equal(summary.score, 100);
  assert.equal(summary.issues.length, 0);
  assert.equal(summary.stats.openPositions, 2);
  assert.equal(summary.stats.strategyGroups, 1);
});

test("flags missing config as critical with strategy owner", () => {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs: [], // no configs
    universe,
    coverage,
    orders,
    services,
  });
  assert.equal(summary.state, "critical");
  const issue = summary.issues.find((i) => i.id === "missing-config:strat_a");
  assert(issue);
  assert.equal(issue!.severity, "critical");
  assert.equal(issue!.owner, "strategy");
  assert(issue!.repairAction);
});

test("flags disabled config as warning", () => {
  const disabledConfigs: StrategyConfigRow[] = [
    { ...configs[0], enabled: false },
  ];
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs: disabledConfigs,
    universe,
    coverage,
    orders,
    services,
  });
  const issue = summary.issues.find((i) => i.id === "config-disabled:strat_a");
  assert(issue);
  assert.equal(issue!.severity, "warning");
  assert.equal(issue!.owner, "strategy");
});

test("flags missing runtime row as warning", () => {
  const summary = buildReconChecklist({
    positions,
    strategies: [], // no runtimes
    configs,
    universe,
    coverage,
    orders,
    services,
  });
  const issue = summary.issues.find((i) => i.id === "no-runtime:strat_a");
  assert(issue);
  assert.equal(issue!.severity, "warning");
  assert.equal(issue!.owner, "strategy");
});

test("flags missing universe as warning with data owner", () => {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe: [], // no universe
    coverage,
    orders,
    services,
  });
  const issue = summary.issues.find((i) => i.id.startsWith("missing-universe:"));
  assert(issue);
  assert.equal(issue!.severity, "warning");
  assert.equal(issue!.owner, "data");
});

test("flags coverage gap as warning with data owner", () => {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage: [], // no coverage
    orders,
    services,
  });
  const issue = summary.issues.find((i) => i.id.startsWith("coverage-gap:"));
  assert(issue);
  assert.equal(issue!.severity, "warning");
  assert.equal(issue!.owner, "data");
});

test("flags rejected orders as critical with risk owner", () => {
  const rejectedOrders: OrderRecord[] = [
    {
      order_id: "o1",
      decision_id: "d1",
      ts_event: 1,
      strategy_id: "strat_a",
      symbol: "AAPL",
      venue: "paper",
      side: "buy",
      order_type: "market",
      quantity: "10",
      status: "rejected",
      filled_qty: "0",
      created_at: 1,
      updated_at: 1,
    },
  ];
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders: rejectedOrders,
    services,
  });
  assert.equal(summary.state, "critical");
  const issue = summary.issues.find((i) => i.id === "rejected-orders");
  assert(issue);
  assert.equal(issue!.severity, "critical");
  assert.equal(issue!.owner, "risk");
});

test("flags pending orders as warning (or critical if >5)", () => {
  const pendingOrders: OrderRecord[] = Array.from({ length: 3 }, (_, i) => ({
    order_id: `o${i}`,
    decision_id: `d${i}`,
    ts_event: 1,
    strategy_id: "strat_a",
    symbol: "AAPL",
    venue: "paper",
    side: "buy",
    order_type: "market",
    quantity: "10",
    status: "new",
    filled_qty: "0",
    created_at: 1,
    updated_at: 1,
  }));
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders: pendingOrders,
    services,
  });
  const issue = summary.issues.find((i) => i.id === "pending-orders");
  assert(issue);
  assert.equal(issue!.severity, "warning");
  assert.equal(issue!.owner, "broker");
});

test("flags service down as critical with operator owner", () => {
  const downServices: ServicesResponse = {
    services: [
      { name: "gbm_predictor", status: "down", last_beat_unix: null, age_sec: null, expected: true },
    ],
    summary: { up: 0, expected: 1, stale_after_sec: 30, ttl_sec: 90 },
  };
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders,
    services: downServices,
  });
  const issue = summary.issues.find((i) => i.id === "service-down:gbm_predictor");
  assert(issue);
  assert.equal(issue!.severity, "critical");
  assert.equal(issue!.owner, "operator");
});

test("builds exportable receipt", () => {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders,
    services,
  });
  const receipt = buildReconReceipt(summary);
  assert.equal(receipt.schema_version, "recon-checklist-receipt.v1");
  assert.equal(receipt.state, "clean");
  assert.equal(receipt.score, 100);
  assert(receipt.exported_at > 0);
});

test("receipt filename includes date and state", () => {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders,
    services,
  });
  const receipt = buildReconReceipt(summary);
  const filename = reconReceiptFilename(receipt);
  assert(filename.startsWith("recon-checklist-"));
  assert(filename.includes("clean"));
  assert(filename.endsWith(".json"));
});

test("empty positions produce clean state", () => {
  const summary = buildReconChecklist({
    positions: [],
    strategies: [],
    configs: [],
    universe: [],
    coverage: [],
    orders: [],
  });
  assert.equal(summary.state, "clean");
  assert.equal(summary.stats.openPositions, 0);
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
  console.log(`${passed} recon checklist tests passed`);
}

run();
