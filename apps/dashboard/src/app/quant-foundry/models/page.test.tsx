import assert from "assert";
import React from "react";

import QuantFoundryModelsPage from "@/app/quant-foundry/models/page";
import type { QuantFoundryDossier } from "@/lib/types";
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

const QUERY_KEY = ["quant-foundry", "dossiers"] as const;

const MOCK_DOSSIERS: QuantFoundryDossier[] = [
  {
    schema_version: 1,
    model_id: "gbm_predictor.v2",
    artifact_manifest_id: "manifest-001",
    artifact_sha256: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
    dataset_manifest_id: "dataset-001",
    feature_schema_hash: "feature_hash_1",
    label_schema_hash: "label_hash_1",
    trial_count: 10,
    training_metrics: { sharpe: 1.5 },
    status: "candidate",
    settlement_evidence_refs: ["evidence-1"],
    shadow_prediction_refs: ["pred-1", "pred-2"],
    blocking_issues: [],
    content_hash: "content_hash_1",
  },
  {
    schema_version: 1,
    model_id: "linear_baseline.v1",
    artifact_manifest_id: "manifest-002",
    artifact_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    dataset_manifest_id: "dataset-002",
    feature_schema_hash: "feature_hash_2",
    label_schema_hash: "label_hash_2",
    trial_count: 5,
    training_metrics: { sharpe: 0.8 },
    status: "rejected",
    settlement_evidence_refs: [],
    shadow_prediction_refs: [],
    blocking_issues: [{ code: "insufficient_evidence", severity: "high", message: "Not enough evidence" }],
    content_hash: "content_hash_2",
  },
];

test("renders without crashing", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_DOSSIERS);
  const html = renderPageWithClient(qc, <QuantFoundryModelsPage />);
  assert(html.includes("Quant Foundry Models"));
});

test("shows loading state", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryLoading(qc, QUERY_KEY);
  const html = renderPageWithClient(qc, <QuantFoundryModelsPage />);
  assert(html.includes("Loading dossiers"));
});

test("shows disabled state when API returns 503 UnavailableError", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createUnavailableError());
  const html = renderPageWithClient(qc, <QuantFoundryModelsPage />);
  assert(html.includes("Quant Foundry is disabled"));
  assert(html.includes("DISABLED"));
});

test("shows error state for non-503 errors", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUERY_KEY, createGenericError("Connection refused"));
  const html = renderPageWithClient(qc, <QuantFoundryModelsPage />);
  assert(html.includes("Unable to load dossiers"));
  assert(html.includes("Connection refused"));
});

test("shows empty state when data is empty array", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, []);
  const html = renderPageWithClient(qc, <QuantFoundryModelsPage />);
  assert(html.includes("No dossiers"));
});

test("shows populated state with dossier rows", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUERY_KEY, MOCK_DOSSIERS);
  const html = renderPageWithClient(qc, <QuantFoundryModelsPage />);
  assert(html.includes("gbm_predictor.v2"));
  assert(html.includes("linear_baseline.v1"));
  assert(html.includes("CANDIDATE"));
  assert(html.includes("REJECTED"));
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
  console.log(`${passed} quant-foundry models page tests passed`);
}

void run();
