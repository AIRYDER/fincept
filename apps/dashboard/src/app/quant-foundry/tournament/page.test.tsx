import assert from "assert";
import React from "react";

import QuantFoundryTournamentPage from "@/app/quant-foundry/tournament/page";
import type { QuantFoundryTournamentEntry } from "@/lib/types";
import {
  createQueryClient,
  createUnavailableError,
  createGenericError,
  renderPageWithClient,
  setAuthToken,
  setQueryData,
  setQueryError,
  setQueryLoading,
} from "@/test/quant-foundry-test-utils";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const QUERY_KEY = ["quant-foundry", "tournament", "leaderboard"] as const;

const MOCK_ENTRIES: QuantFoundryTournamentEntry[] = [
  {
    model_id: "gbm_predictor.v2",
    total_score: 1.2345,
    settled_count: 15,
    horizon_slices: [],
    regime_slices: [],
    symbol_cluster_slices: [],
    baseline_delta: {
      baseline_model_id: "linear_baseline.v1",
      delta: 0.3,
      baseline_score: 0.9345,
    },
    calibration_summary: null,
    decay_indicator: null,
  },
  {
    model_id: "linear_baseline.v1",
    total_score: 0.9345,
    settled_count: 15,
    horizon_slices: [],
    regime_slices: [],
    symbol_cluster_slices: [],
    baseline_delta: null,
    calibration_summary: null,
    decay_indicator: {
      decay_score: 0.6,
      is_stale: true,
      is_decayed: false,
      days_since_last_settlement: 7,
    },
  },
];

test("renders without crashing", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_ENTRIES);
  const html = renderPageWithClient(qc, <QuantFoundryTournamentPage />);
  assert(html.includes("Quant Foundry Tournament"));
});

test("shows loading state", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryLoading(qc, QUERY_KEY);
  const html = renderPageWithClient(qc, <QuantFoundryTournamentPage />);
  assert(html.includes("Loading leaderboard"));
});

test("shows disabled state when API returns 503 UnavailableError", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createUnavailableError());
  const html = renderPageWithClient(qc, <QuantFoundryTournamentPage />);
  assert(html.includes("Quant Foundry is disabled"));
  assert(html.includes("DISABLED"));
});

test("shows error state for non-503 errors", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createGenericError("Connection refused"));
  const html = renderPageWithClient(qc, <QuantFoundryTournamentPage />);
  assert(html.includes("Unable to load leaderboard"));
  assert(html.includes("Connection refused"));
});

test("shows empty state when data is empty array", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, []);
  const html = renderPageWithClient(qc, <QuantFoundryTournamentPage />);
  assert(html.includes("No leaderboard entries"));
});

test("shows populated state with leaderboard rows", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_ENTRIES);
  const html = renderPageWithClient(qc, <QuantFoundryTournamentPage />);
  assert(html.includes("gbm_predictor.v2"));
  assert(html.includes("linear_baseline.v1"));
  assert(html.includes("1.2345"));
  // baseline delta for first entry
  assert(html.includes("+0.3000"));
  // decay status: second entry is stale
  assert(html.includes("STALE"));
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
  console.log(`${passed} quant-foundry tournament page tests passed`);
}

void run();
