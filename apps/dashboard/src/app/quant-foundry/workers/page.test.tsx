import assert from "assert";
import React from "react";

import QuantFoundryWorkerHealthPage from "@/app/quant-foundry/workers/page";
import type { QuantFoundryWorkerHealth } from "@/lib/types";
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

const QUERY_KEY = ["quant-foundry", "worker-health"] as const;

const NOW = Math.floor(Date.now() / 1000);

const MOCK_HEALTH: QuantFoundryWorkerHealth = {
  enabled: true,
  worker_status_dir: "/var/lib/quant-foundry/worker-status",
  stale_threshold_seconds: 120,
  heartbeats: [
    {
      job_id: "job-001",
      status: "running",
      heartbeat_at: NOW - 5,
      updated_at: NOW - 5,
    },
    {
      job_id: "job-002",
      status: "running",
      heartbeat_at: NOW - 30,
      updated_at: NOW - 30,
    },
  ],
  stale_workers: [],
  stale_count: 0,
  total_workers: 2,
};

const MOCK_HEALTH_WITH_STALE: QuantFoundryWorkerHealth = {
  enabled: true,
  worker_status_dir: "/var/lib/quant-foundry/worker-status",
  stale_threshold_seconds: 120,
  heartbeats: [
    {
      job_id: "job-001",
      status: "running",
      heartbeat_at: NOW - 5,
      updated_at: NOW - 5,
    },
    {
      job_id: "job-stale-001",
      status: "stale",
      heartbeat_at: NOW - 600,
      updated_at: NOW - 600,
    },
  ],
  stale_workers: [
    {
      job_id: "job-stale-001",
      status: "stale",
      heartbeat_at: NOW - 600,
      updated_at: NOW - 600,
    },
  ],
  stale_count: 1,
  total_workers: 2,
};

test("renders without crashing", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_HEALTH);
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("Worker Health"));
});

test("shows loading state", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryLoading(qc, QUERY_KEY);
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("Loading worker health"));
});

test("shows disabled state when API returns 503 UnavailableError", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createUnavailableError());
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("Quant Foundry is disabled"));
  assert(html.includes("DISABLED"));
});

test("shows error state for non-503 errors", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createGenericError("Connection refused"));
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("Unable to load worker health"));
  assert(html.includes("Connection refused"));
});

test("shows populated state with worker data", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_HEALTH);
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("Worker heartbeat summary"));
  assert(html.includes("Total workers"));
  assert(html.includes("2"));
  assert(html.includes("Stale threshold (s)"));
  assert(html.includes("120"));
  assert(html.includes("Worker status dir"));
  assert(html.includes("/var/lib/quant-foundry/worker-status"));
  assert(html.includes("job-001"));
  assert(html.includes("job-002"));
  assert(html.includes("RUNNING"));
});

test("shows empty state when no heartbeats present", () => {
  setAuthToken();
  const qc = createQueryClient();
  const emptyHealth: QuantFoundryWorkerHealth = {
    ...MOCK_HEALTH,
    heartbeats: [],
    stale_workers: [],
    total_workers: 0,
    stale_count: 0,
    worker_status_dir: null,
  };
  setQueryData(qc, QUERY_KEY, emptyHealth);
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("No heartbeats yet"));
  assert(html.includes("not configured"));
});

test("shows stale workers when present", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_HEALTH_WITH_STALE);
  const html = renderPageWithClient(qc, <QuantFoundryWorkerHealthPage />);
  assert(html.includes("Stale workers"));
  assert(html.includes("STALE"));
  assert(html.includes("job-stale-001"));
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
  console.log(`${passed} quant-foundry workers page tests passed`);
}

void run();
