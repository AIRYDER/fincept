import assert from "assert";

import type {
  DataCoverageResponse,
  DataSourceDefinition,
  DataSourcesResponse,
  OpenBBHealthResponse,
  ProviderDataResponse,
  ServicesResponse,
} from "@/lib/types";

import { buildSourceHealthSummary } from "./source-health";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const source: DataSourceDefinition = {
  id: "openbb",
  name: "OpenBB",
  area: "Provider/capability browser",
  category: "market_data",
  safety: "read_only",
  status: "registered",
  call_surfaces: ["GET /research/openbb/health"],
  data: ["quotes"],
  return_format: "normalized_rows",
  latency: "local_api_plus_provider",
  health: { mode: "active_probe", checks: ["openapi", "provider"] },
  config: ["OPENBB_API_URL"],
};

const sources: DataSourcesResponse = {
  sources: [source],
  summary: { total: 1, by_category: { market_data: 1 } },
};

const coverage: DataCoverageResponse = {
  freq: "1m",
  venue: null,
  as_of_ns: 1,
  lookback_ns: 1,
  stale_after_ns: 1,
  summary: { total: 10, ok: 10, stale: 0, empty: 0, error: 0, coverage_pct: 100 },
  rows: [],
};

const openbb: OpenBBHealthResponse = {
  ok: true,
  url: "http://127.0.0.1:6900",
  latency_ms: 12,
};

const services: ServicesResponse = {
  services: [
    { name: "market_data", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
    { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
  ],
  summary: { up: 2, expected: 2, stale_after_sec: 30, ttl_sec: 90 },
};

const providerData: ProviderDataResponse = {
  ok: true,
  capture_enabled: true,
  summary: {
    total_records: 2,
    ok_records: 2,
    error_records: 0,
    latest_ts_event: 1_000,
    providers: { openbb: 1, exa: 1 },
    datasets: { "equity.price.quote": 1, research_brief: 1 },
  },
  records: [
    {
      record_id: "provider-record-1",
      schema_version: "provider-data.v1",
      provider: "openbb",
      source: "research.openbb_quote",
      dataset: "equity.price.quote",
      endpoint: "POST /research/openbb/quote",
      symbol: "NVDA",
      ts_event: 1_000,
      ts_observed: null,
      request_hash: "hash",
      row_count: 1,
      ok: true,
      error_type: null,
      normalized: {},
    },
  ],
};

test("builds a ready source health summary", () => {
  const summary = buildSourceHealthSummary({ sources, coverage, openbb, providerData, services });
  assert.equal(summary.state, "ready");
  assert.equal(summary.checks.every((check) => check.severity === "pass"), true);
  assert.equal(summary.registryRows.length, 1);
  assert.equal(summary.captureRows.length, 1);
});

test("blocks when coverage is unavailable", () => {
  const summary = buildSourceHealthSummary({ sources, coverage: null, openbb, providerData, services });
  assert.equal(summary.state, "blocked");
  assert(summary.checks.some((check) => check.id === "coverage" && check.severity === "fail"));
});

test("blocks when expected services are down", () => {
  const summary = buildSourceHealthSummary({
    sources,
    coverage,
    openbb,
    providerData,
    services: {
      ...services,
      services: [{ name: "market_data", status: "down", last_beat_unix: null, age_sec: null, expected: true }],
      summary: { up: 0, expected: 1, stale_after_sec: 30, ttl_sec: 90 },
    },
  });
  assert.equal(summary.state, "blocked");
  assert(summary.checks.some((check) => check.id === "services" && check.severity === "fail"));
});

test("marks partial coverage and OpenBB warning for review", () => {
  const summary = buildSourceHealthSummary({
    sources,
    coverage: {
      ...coverage,
      summary: { total: 10, ok: 8, stale: 2, empty: 0, error: 0, coverage_pct: 80 },
    },
    openbb: { ...openbb, warning: "provider keys missing" },
    providerData,
    services,
  });
  assert.equal(summary.state, "review");
  assert(summary.checks.some((check) => check.id === "coverage" && check.severity === "watch"));
  assert(summary.checks.some((check) => check.id === "openbb" && check.severity === "watch"));
});

test("marks disabled provider capture for review", () => {
  const summary = buildSourceHealthSummary({
    sources,
    coverage,
    openbb,
    providerData: {
      ok: false,
      capture_enabled: false,
      error_type: "ProviderDataDisabled",
      summary: {
        total_records: 0,
        ok_records: 0,
        error_records: 0,
        latest_ts_event: null,
        providers: {},
        datasets: {},
      },
      records: [],
    },
    services,
  });
  assert.equal(summary.state, "review");
  assert(summary.checks.some((check) => check.id === "provider-capture" && check.severity === "watch"));
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
  console.log(`${passed} source health tests passed`);
}

void run();
