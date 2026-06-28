import assert from "assert";
import React from "react";

import QuantFoundryShadowHealthPage from "@/app/quant-foundry/shadow/page";
import type { QuantFoundryShadowHealth } from "@/lib/types";
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

const QUERY_KEY = ["quant-foundry", "shadow", "health"] as const;

const MOCK_HEALTH: QuantFoundryShadowHealth = {
  enabled: true,
  models_running: 3,
  latest_prediction_ts: 1_700_000_000,
  latency_p50_ms: 12.5,
  latency_p95_ms: 45.2,
  feature_availability: 0.98,
  callback_rejection_rate: null,
  settlement_lag_seconds: 30.5,
  circuit_breaker_state: "closed",
  prediction_count: 1500,
  settled_count: 1200,
};

test("renders without crashing", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_HEALTH);
  const html = renderPageWithClient(qc, <QuantFoundryShadowHealthPage />);
  assert(html.includes("Shadow Inference Health"));
});

test("shows loading state", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryLoading(qc, QUERY_KEY);
  const html = renderPageWithClient(qc, <QuantFoundryShadowHealthPage />);
  assert(html.includes("Loading shadow health"));
});

test("shows disabled state when API returns 503 UnavailableError", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createUnavailableError());
  const html = renderPageWithClient(qc, <QuantFoundryShadowHealthPage />);
  assert(html.includes("Quant Foundry is disabled"));
  assert(html.includes("DISABLED"));
});

test("shows error state for non-503 errors", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createGenericError("Connection refused"));
  const html = renderPageWithClient(qc, <QuantFoundryShadowHealthPage />);
  assert(html.includes("Unable to load shadow health"));
  assert(html.includes("Connection refused"));
});

test("shows populated state with health metrics", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_HEALTH);
  const html = renderPageWithClient(qc, <QuantFoundryShadowHealthPage />);
  assert(html.includes("Shadow prediction surface"));
  assert(html.includes("Models running"));
  assert(html.includes("3"));
  assert(html.includes("Prediction count"));
  assert(html.includes("1500"));
  assert(html.includes("Latency p50"));
  assert(html.includes("12.50"));
  assert(html.includes("Circuit-breaker state"));
  assert(html.includes("CLOSED"));
});

test("shows populated state with null metrics rendered as em dash", () => {
  setAuthToken();
  const qc = createQueryClient();
  const healthWithNulls: QuantFoundryShadowHealth = {
    ...MOCK_HEALTH,
    enabled: false,
    latency_p50_ms: null,
    latency_p95_ms: null,
    feature_availability: null,
    settlement_lag_seconds: null,
    callback_rejection_rate: null,
    latest_prediction_ts: null,
    prediction_count: 0,
    settled_count: 0,
    circuit_breaker_state: "open",
  };
  setQueryData(qc, QUERY_KEY, healthWithNulls);
  const html = renderPageWithClient(qc, <QuantFoundryShadowHealthPage />);
  // Null latency should render as em dash
  assert(html.includes("—"));
  // Circuit breaker open should show OPEN
  assert(html.includes("OPEN"));
  // Disabled health should show DISABLED pill
  assert(html.includes("DISABLED"));
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
  console.log(`${passed} quant-foundry shadow page tests passed`);
}

void run();
