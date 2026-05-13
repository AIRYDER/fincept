import assert from "assert";

import type { Position, StrategyConfigRow } from "@/lib/types";

import { buildStrategyReadiness } from "./strategy-readiness";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const baseConfig: StrategyConfigRow = {
  strategy_id: "demo-ma",
  class_name: "ma_crossover",
  symbols: ["SPY", "QQQ"],
  params: { fast: 5, slow: 20, per_symbol_notional: 10000 },
  model_binding: null,
  enabled: false,
  created_at: 1,
  updated_at: 2,
};

const openPosition: Position = {
  strategy_id: "demo-ma",
  symbol: "SPY",
  quantity: "10",
  avg_cost: "100",
  realized_pnl: "0",
  unrealized_pnl: "25",
  updated_at: 3,
};

test("builds a review-ready strategy readiness packet for a stopped valid config", () => {
  const readiness = buildStrategyReadiness(baseConfig, []);
  assert.equal(readiness.state, "review");
  assert(readiness.score > 0 && readiness.score <= 100);
  assert(readiness.checks.some((check) => check.id === "symbols" && check.severity === "pass"));
  assert(readiness.checks.some((check) => check.id === "params" && check.severity === "pass"));
});

test("blocks readiness when symbols are missing", () => {
  const readiness = buildStrategyReadiness({ ...baseConfig, symbols: [] }, []);
  assert.equal(readiness.state, "blocked");
  assert(readiness.checks.some((check) => check.id === "symbols" && check.severity === "fail"));
});

test("blocks gbm readiness without an explicit model binding", () => {
  const readiness = buildStrategyReadiness(
    {
      ...baseConfig,
      class_name: "gbm",
      params: { entry_threshold: 0.1, exit_threshold: 0.1, per_symbol_notional: 5000 },
      model_binding: null,
    },
    [],
  );
  assert.equal(readiness.state, "blocked");
  assert(readiness.checks.some((check) => check.id === "model-binding" && check.severity === "fail"));
});

test("flags position drift as a review item", () => {
  const readiness = buildStrategyReadiness(baseConfig, [openPosition]);
  assert.equal(readiness.state, "review");
  assert(readiness.checks.some((check) => check.id === "position-drift" && check.severity === "watch"));
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
  console.log(`${passed} strategy readiness tests passed`);
}

void run();
