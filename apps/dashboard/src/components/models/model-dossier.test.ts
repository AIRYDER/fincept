import assert from "assert";

import type { ModelRecord, PromotionStateResponse } from "@/lib/types";

import { buildModelDossier } from "./model-dossier";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const baseModel: ModelRecord = {
  name: "gbm_candidate_v1",
  path: "models/gbm_candidate_v1",
  model_file_exists: true,
  trained_at_unix: 1,
  age_seconds: 3600,
  eval_mode: "walk_forward",
  horizon_bars: 5,
  horizon_ns: 300_000_000_000,
  bar_seconds: 60,
  features: ["ret_1", "vol_20"],
  feature_count: 2,
  cv_summary: {
    n_folds: 5,
    n_scored: 5,
    n_skipped: 0,
    median_best_iter: 80,
    mean_auc: 0.58,
    std_auc: 0.02,
    min_auc: 0.54,
    max_auc: 0.61,
  },
  cv_folds: null,
  purge_bars: 5,
  embargo_bars: 1,
  final_train_rows: 10000,
  final_num_boost_round: 80,
  holdout_auc: null,
  holdout_rows: null,
  training_input_path: "data/features.parquet",
  training_request: null,
  warnings: [],
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

test("builds a ready model dossier for a walk-forward active model", () => {
  const dossier = buildModelDossier(baseModel, promotion);
  assert.equal(dossier.state, "ready");
  assert.equal(dossier.checks.every((check) => check.severity === "pass"), true);
  assert(dossier.evidence.some((row) => row.label === "active" && row.value === baseModel.name));
});

test("blocks a dossier when the artifact is missing", () => {
  const dossier = buildModelDossier({ ...baseModel, model_file_exists: false }, promotion);
  assert.equal(dossier.state, "blocked");
  assert(dossier.checks.some((check) => check.id === "artifact" && check.severity === "fail"));
});

test("requires a useful AUC before promotion review", () => {
  const dossier = buildModelDossier(
    {
      ...baseModel,
      cv_summary: { ...baseModel.cv_summary!, mean_auc: 0.5 },
    },
    promotion,
  );
  assert.equal(dossier.state, "blocked");
  assert(dossier.checks.some((check) => check.id === "predictive-quality" && check.severity === "fail"));
});

test("marks legacy holdout models for review instead of ready", () => {
  const dossier = buildModelDossier(
    {
      ...baseModel,
      eval_mode: "holdout_80_20",
      cv_summary: null,
      holdout_auc: 0.57,
      holdout_rows: 5000,
    },
    null,
  );
  assert.equal(dossier.state, "review");
  assert(dossier.checks.some((check) => check.id === "evaluation-mode" && check.severity === "watch"));
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
  console.log(`${passed} model dossier tests passed`);
}

void run();
