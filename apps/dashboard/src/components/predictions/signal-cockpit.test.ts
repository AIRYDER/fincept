import assert from "assert";

import type { Prediction, PromotionStateResponse, ServicesResponse } from "@/lib/types";

import { buildSignalCockpit } from "./signal-cockpit";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const nowMs = 1_700_000_000_000;

function makePrediction(overrides: Partial<Prediction> = {}): Prediction {
  return {
    agent_id: "gbm_predictor.v1",
    symbol: "NVDA",
    ts_event: Math.floor(nowMs / 1000) - 60,
    horizon_ns: 60_000_000_000,
    direction: 0.55,
    confidence: 0.82,
    calibration_tag: "gbm.v1",
    ...overrides,
  };
}

const services: ServicesResponse = {
  services: [
    { name: "gbm_predictor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
    { name: "news_alpha_predictor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
  ],
  summary: { up: 2, expected: 2, stale_after_sec: 30, ttl_sec: 90 },
};

const promotion: PromotionStateResponse = {
  agent_id: "gbm_predictor.v1",
  active: {
    agent_id: "gbm_predictor.v1",
    model_name: "gbm_candidate_v1",
    promoted_at: 1,
    promoted_by: "operator",
  },
  shadow: null,
  history: [],
};

test("builds a ready production signal cockpit for fresh consensus", () => {
  const cockpit = buildSignalCockpit({
    predictions: [makePrediction(), makePrediction({ agent_id: "news_alpha_predictor.v1", direction: 0.35, confidence: 0.74 })],
    services,
    promotion,
    nowMs,
  });
  assert.equal(cockpit.state, "ready");
  assert.equal(cockpit.symbols[0].state, "candidate");
  assert.equal(cockpit.stats.highConfidenceCount, 2);
});

test("marks an empty browser signal feed for review", () => {
  const cockpit = buildSignalCockpit({ predictions: [], services, promotion, nowMs });
  assert.equal(cockpit.state, "review");
  assert(cockpit.checks.some((check) => check.id === "signal-feed" && check.severity === "watch"));
});

test("blocks stale production signals", () => {
  const cockpit = buildSignalCockpit({
    predictions: [makePrediction({ ts_event: Math.floor(nowMs / 1000) - 3600 })],
    services,
    promotion,
    nowMs,
  });
  assert.equal(cockpit.state, "blocked");
  assert(cockpit.checks.some((check) => check.id === "freshness" && check.severity === "fail"));
});

test("blocks when expected predictor service is down", () => {
  const cockpit = buildSignalCockpit({
    predictions: [makePrediction(), makePrediction({ agent_id: "news_alpha_predictor.v1" })],
    services: {
      ...services,
      services: [{ name: "gbm_predictor", status: "down", last_beat_unix: null, age_sec: null, expected: true }],
      summary: { up: 0, expected: 1, stale_after_sec: 30, ttl_sec: 90 },
    },
    promotion,
    nowMs,
  });
  assert.equal(cockpit.state, "blocked");
  assert(cockpit.checks.some((check) => check.id === "predictor-services" && check.severity === "fail"));
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
  console.log(`${passed} signal cockpit tests passed`);
}

void run();
