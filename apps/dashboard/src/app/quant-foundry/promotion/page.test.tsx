import assert from "assert";
import React from "react";

import QuantFoundryPromotionPage from "@/app/quant-foundry/promotion/page";
import type {
  QuantFoundryPromotionQueueEntry,
  QuantFoundryPromotionReview,
} from "@/lib/types";
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

const QUEUE_KEY = ["quant-foundry", "promotion", "queue"] as const;
const COMPLETED_KEY = ["quant-foundry", "promotion", "completed"] as const;

const MOCK_QUEUE_ENTRY: QuantFoundryPromotionQueueEntry = {
  request: {
    model_id: "gbm_predictor.v2",
    target_level: "shadow_approved",
    review_note: "Ready for shadow promotion",
    waivers: [],
  },
  evidence: {
    dossier: null,
    tournament_result: null,
    sentinel_receipt: null,
    blocking_issues: [],
  },
};

const MOCK_COMPLETED_REVIEW: QuantFoundryPromotionReview = {
  decision: "approved",
  request: {
    model_id: "linear_baseline.v1",
    target_level: "research_approved",
    review_note: "Approved for research",
    waivers: [],
  },
  review_note: "Approved for research",
  rejection_reason: null,
  decided_at_ns: 1_700_000_000_000_000_000,
};

test("renders without crashing", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUEUE_KEY, [MOCK_QUEUE_ENTRY]);
  setQueryData(qc, COMPLETED_KEY, [MOCK_COMPLETED_REVIEW]);
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  assert(html.includes("Quant Foundry Promotion"));
});

test("shows loading state", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryLoading(qc, QUEUE_KEY);
  setQueryLoading(qc, COMPLETED_KEY);
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  assert(html.includes("Loading review queue"));
});

test("shows disabled state when API returns 503 UnavailableError", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUEUE_KEY, createUnavailableError());
  setQueryError(qc, COMPLETED_KEY, createUnavailableError());
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  assert(html.includes("Quant Foundry is disabled"));
  assert(html.includes("DISABLED"));
});

test("shows error state for non-503 errors", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryError(qc, QUEUE_KEY, createGenericError("Connection refused"));
  setQueryData(qc, COMPLETED_KEY, []);
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  assert(html.includes("Unable to load promotion queue"));
  assert(html.includes("Connection refused"));
});

test("shows empty state when queue and completed are empty arrays", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUEUE_KEY, []);
  setQueryData(qc, COMPLETED_KEY, []);
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  assert(html.includes("No pending reviews"));
  assert(html.includes("No completed promotions"));
});

test("shows populated state with pending review and completed receipt", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUEUE_KEY, [MOCK_QUEUE_ENTRY]);
  setQueryData(qc, COMPLETED_KEY, [MOCK_COMPLETED_REVIEW]);
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  assert(html.includes("gbm_predictor.v2"));
  assert(html.includes("shadow_approved"));
  assert(html.includes("linear_baseline.v1"));
  assert(html.includes("APPROVED"));
});

test("verify that approve and reject buttons exist in pending review", () => {
  setAuthToken();
  const qc = createQueryClient();
  setQueryData(qc, QUEUE_KEY, [MOCK_QUEUE_ENTRY]);
  setQueryData(qc, COMPLETED_KEY, []);
  const html = renderPageWithClient(qc, <QuantFoundryPromotionPage />);
  // The PendingReviewCard renders two ReviewDialog triggers:
  // one with text "approve" and one with text "reject".
  assert(html.includes("approve"), "expected approve button in pending review");
  assert(html.includes("reject"), "expected reject button in pending review");
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
  console.log(`${passed} quant-foundry promotion page tests passed`);
}

void run();
