import assert from "assert";
import React from "react";

import QuantFoundryJobsPage from "@/app/quant-foundry/jobs/page";
import type { QuantFoundryJob } from "@/lib/types";
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

const QUERY_KEY = ["quant-foundry", "jobs", "all"] as const;

const MOCK_JOBS: QuantFoundryJob[] = [
  {
    job_id: "job-001",
    job_type: "training",
    status: "completed",
    priority: 5,
    created_at_ns: 1_700_000_000_000_000_000,
    updated_at_ns: 1_700_000_001_000_000_000,
  },
  {
    job_id: "job-002",
    job_type: "inference",
    status: "running",
    priority: 3,
    created_at_ns: 1_700_000_002_000_000_000,
  },
];

test("renders without crashing", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_JOBS);
  const html = renderPageWithClient(qc, <QuantFoundryJobsPage />);
  assert(html.includes("Quant Foundry Jobs"));
});

test("shows loading state", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryLoading(qc, QUERY_KEY);
  const html = renderPageWithClient(qc, <QuantFoundryJobsPage />);
  assert(html.includes("Loading jobs"));
});

test("shows disabled state when API returns 503 UnavailableError", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createUnavailableError());
  const html = renderPageWithClient(qc, <QuantFoundryJobsPage />);
  assert(html.includes("Quant Foundry is disabled"));
  assert(html.includes("DISABLED"));
});

test("shows error state for non-503 errors", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createGenericError("Connection refused"));
  const html = renderPageWithClient(qc, <QuantFoundryJobsPage />);
  assert(html.includes("Unable to load jobs"));
  assert(html.includes("Connection refused"));
});

test("shows empty state when data is empty array", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, []);
  const html = renderPageWithClient(qc, <QuantFoundryJobsPage />);
  assert(html.includes("No jobs"));
});

test("shows populated state with job rows", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_JOBS);
  const html = renderPageWithClient(qc, <QuantFoundryJobsPage />);
  assert(html.includes("job-001"));
  assert(html.includes("job-002"));
  assert(html.includes("training"));
  assert(html.includes("inference"));
  assert(html.includes("COMPLETED"));
  assert(html.includes("RUNNING"));
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
  console.log(`${passed} quant-foundry jobs page tests passed`);
}

void run();
