import assert from "assert";

import type { BacktestManifest, BacktestReport } from "@/lib/types";

import {
  backtestLabReceiptFilename,
  buildBacktestLab,
  buildBacktestLabReceipt,
} from "./backtest-lab";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const report: BacktestReport = {
  starting_cash: 100_000,
  final_equity: 105_000,
  total_return_pct: 5.0,
  n_bars: 15_000,
  n_fills: 24,
  fees_paid_total: 120.5,
  sharpe: 1.8,
  max_drawdown_pct: -3.2,
  longest_drawdown_bars: 500,
  bars_per_year: 525_600,
  per_symbol: [
    { symbol: "AAPL", fills: 12, bought_qty: 600, sold_qty: 600, notional_traded: 132_000, fees_paid: 66.0 },
    { symbol: "NVDA", fills: 8, bought_qty: 200, sold_qty: 200, notional_traded: 88_000, fees_paid: 44.0 },
    { symbol: "SPY", fills: 4, bought_qty: 100, sold_qty: 100, notional_traded: 45_000, fees_paid: 10.5 },
  ],
  equity_curve: [],
  trades: [],
};

const manifest: BacktestManifest = {
  run_id: "abc123def456",
  status: "complete",
  started_at: 1_700_000_000,
  finished_at: 1_700_000_100,
  parquet_path: "data/synth_ohlcv.parquet",
  strategy_name: "ma_crossover",
  strategy_params: { fast: 5, slow: 30, per_symbol_notional: 10000 },
  starting_cash: 100_000,
  freq: "1m",
  venue: "paper",
  asset_class: "crypto_spot",
  bars_per_year: 525_600,
  symbols: ["AAPL", "NVDA", "SPY"],
  start_ns: 0,
  end_ns: 1_000_000,
  n_bars: 15_000,
  n_fills: 24,
  final_equity: 105_000,
  total_return_pct: 5.0,
  sharpe: 1.8,
  max_drawdown_pct: -3.2,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("builds a review summary with healthy report (risk-gate always watch)", () => {
  const summary = buildBacktestLab({ report, manifest });
  // Risk-gate is always "watch" since it's not simulated in backtest,
  // so even a healthy run lands in "review" state.
  assert.equal(summary.state, "review");
  assert.equal(summary.stats.hasReport, true);
  assert.equal(summary.stats.nFills, 24);
  assert.equal(summary.stats.symbolsTraded, 3);
  assert(summary.checks.some((c) => c.id === "report" && c.severity === "pass"));
  assert(summary.checks.some((c) => c.id === "fills" && c.severity === "pass"));
  assert(summary.checks.some((c) => c.id === "manifest" && c.severity === "pass"));
  assert(summary.checks.some((c) => c.id === "risk-gate" && c.severity === "watch"));
});

test("blocks when no report is loaded", () => {
  const summary = buildBacktestLab({ report: null, manifest: null });
  assert.equal(summary.state, "blocked");
  assert(summary.checks.some((c) => c.id === "report" && c.severity === "fail"));
});

test("marks review when fills are zero", () => {
  const noFillsReport: BacktestReport = { ...report, n_fills: 0, per_symbol: [] };
  const summary = buildBacktestLab({ report: noFillsReport, manifest });
  assert.equal(summary.state, "review");
  assert(summary.checks.some((c) => c.id === "fills" && c.severity === "watch"));
});

test("marks review when fee impact is high", () => {
  const highFeeReport: BacktestReport = {
    ...report,
    fees_paid_total: 3_000,
    per_symbol: [
      { symbol: "AAPL", fills: 100, bought_qty: 5000, sold_qty: 5000, notional_traded: 1_100_000, fees_paid: 1_500 },
      { symbol: "NVDA", fills: 100, bought_qty: 5000, sold_qty: 5000, notional_traded: 1_100_000, fees_paid: 1_500 },
    ],
  };
  const summary = buildBacktestLab({ report: highFeeReport, manifest });
  assert(summary.checks.some((c) => c.id === "fees" && c.severity === "watch"));
  assert(summary.stats.feeImpactPct > 2);
});

test("warns about zero fees when fills exist", () => {
  const zeroFeeReport: BacktestReport = {
    ...report,
    fees_paid_total: 0,
    per_symbol: [
      { symbol: "AAPL", fills: 10, bought_qty: 500, sold_qty: 500, notional_traded: 110_000, fees_paid: 0 },
    ],
  };
  const summary = buildBacktestLab({ report: zeroFeeReport, manifest });
  assert(summary.checks.some((c) => c.id === "zero-fees" && c.severity === "watch"));
});

test("always warns about risk gate not being simulated", () => {
  const summary = buildBacktestLab({ report, manifest });
  assert(summary.checks.some((c) => c.id === "risk-gate" && c.severity === "watch"));
});

test("computes correct attribution", () => {
  const summary = buildBacktestLab({ report, manifest });
  assert.equal(summary.attribution.length, 3);
  // Sorted by notionalTraded desc
  assert.equal(summary.attribution[0].symbol, "AAPL");
  assert.equal(summary.attribution[0].notionalTraded, 132_000);
  assert(summary.attribution[0].feeBps > 0);
  assert(summary.attribution[0].pctOfTotalNotional > 0);
});

test("computes turnover ratio correctly", () => {
  const summary = buildBacktestLab({ report, manifest });
  // total notional = 132k + 88k + 45k = 265k, starting_cash = 100k
  // turnover = 265k / 100k = 2.65
  assert(Math.abs(summary.stats.turnoverRatio - 2.65) < 0.01);
});

test("builds exportable receipt", () => {
  const receipt = buildBacktestLabReceipt({ report, manifest });
  assert(receipt !== null);
  assert.equal(receipt.schema_version, "backtest-lab-receipt.v1");
  assert.equal(receipt.run_id, "abc123def456");
  assert.equal(receipt.manifest.strategy, "ma_crossover");
  assert.equal(receipt.metrics.fees_paid_total, 120.5);
  assert.equal(receipt.attribution.length, 3);
  assert(receipt.assumptions.length > 0);
});

test("returns null receipt when report or manifest missing", () => {
  assert.equal(buildBacktestLabReceipt({ report: null, manifest }), null);
  assert.equal(buildBacktestLabReceipt({ report, manifest: null }), null);
});

test("receipt filename includes run id and date", () => {
  const receipt = buildBacktestLabReceipt({ report, manifest })!;
  const filename = backtestLabReceiptFilename(receipt);
  assert(filename.startsWith("backtest-lab-abc123def456"));
  assert(filename.endsWith(".json"));
});

test("assumptions include strategy, freq, venue, and risk gate warning", () => {
  const summary = buildBacktestLab({ report, manifest });
  assert(summary.assumptions.some((a) => a.includes("ma_crossover")));
  assert(summary.assumptions.some((a) => a.includes("1m")));
  assert(summary.assumptions.some((a) => a.includes("Risk gate")));
});

test("marks review when manifest is missing", () => {
  const summary = buildBacktestLab({ report, manifest: null });
  assert(summary.checks.some((c) => c.id === "manifest" && c.severity === "watch"));
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
  console.log(`${passed} backtest lab tests passed`);
}

run();
