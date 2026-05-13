import type { ActiveBinding, ModelRecord, PromotionStateResponse } from "@/lib/types";

export type ModelDossierState = "ready" | "review" | "blocked";
export type ModelDossierSeverity = "pass" | "watch" | "fail";

export interface ModelDossierCheck {
  id: string;
  label: string;
  severity: ModelDossierSeverity;
  detail: string;
}

export interface ModelDossierSummary {
  state: ModelDossierState;
  score: number;
  headline: string;
  checks: ModelDossierCheck[];
  actions: string[];
  evidence: Array<{ label: string; value: string; severity: ModelDossierSeverity }>;
}

export function buildModelDossier(
  model: ModelRecord,
  promotion?: PromotionStateResponse | null,
): ModelDossierSummary {
  const checks = buildChecks(model, promotion ?? null);
  const failed = checks.filter((check) => check.severity === "fail").length;
  const watches = checks.filter((check) => check.severity === "watch").length;
  const state: ModelDossierState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const score = clamp(100 - failed * 26 - watches * 9, 0, 100);

  return {
    state,
    score,
    headline: headlineFor(state, failed, watches),
    checks,
    actions: buildActions(state, checks),
    evidence: buildEvidence(model, promotion ?? null, checks),
  };
}

function buildChecks(model: ModelRecord, promotion: PromotionStateResponse | null): ModelDossierCheck[] {
  const auc = primaryAuc(model);
  const active = promotion?.active ?? null;
  const shadow = promotion?.shadow ?? null;

  return [
    {
      id: "artifact",
      label: "Artifact",
      severity: model.model_file_exists ? "pass" : "fail",
      detail: model.model_file_exists ? "model.txt exists and can be loaded for inference." : "model.txt is missing; promotion would bind an unloadable artifact.",
    },
    {
      id: "evaluation-mode",
      label: "Evaluation mode",
      severity: model.eval_mode === "walk_forward" ? "pass" : model.eval_mode === "holdout_80_20" ? "watch" : "fail",
      detail: model.eval_mode === "walk_forward" ? "Walk-forward validation metadata is present." : model.eval_mode === "holdout_80_20" ? "Legacy holdout split; review before promotion." : "No evaluation mode recorded.",
    },
    {
      id: "predictive-quality",
      label: "Predictive quality",
      severity: auc == null ? "fail" : auc >= 0.55 ? "pass" : auc >= 0.515 ? "watch" : "fail",
      detail: auc == null ? "No AUC metric recorded." : `Primary AUC is ${auc.toFixed(3)}.`,
    },
    {
      id: "stability",
      label: "Fold stability",
      severity: stabilitySeverity(model),
      detail: stabilityDetail(model),
    },
    {
      id: "features",
      label: "Feature coverage",
      severity: model.feature_count > 0 ? "pass" : "fail",
      detail: model.feature_count > 0 ? `${model.feature_count} feature(s) recorded.` : "No features recorded in metadata.",
    },
    {
      id: "warnings",
      label: "Warnings",
      severity: model.warnings.length > 0 ? "watch" : "pass",
      detail: model.warnings.length > 0 ? `${model.warnings.length} warning(s) require review.` : "No model warnings recorded.",
    },
    {
      id: "promotion-state",
      label: "Promotion state",
      severity: promotionStateSeverity(model.name, active, shadow),
      detail: promotionStateDetail(model.name, active, shadow),
    },
  ];
}

function buildEvidence(
  model: ModelRecord,
  promotion: PromotionStateResponse | null,
  checks: ModelDossierCheck[],
): ModelDossierSummary["evidence"] {
  const passCount = checks.filter((check) => check.severity === "pass").length;
  return [
    { label: "checks", value: `${passCount}/${checks.length} pass`, severity: checks.some((check) => check.severity === "fail") ? "fail" : "pass" },
    { label: "model", value: model.name, severity: "pass" },
    { label: "primary auc", value: primaryAuc(model)?.toFixed(3) ?? "missing", severity: primaryAuc(model) == null ? "fail" : "pass" },
    { label: "eval mode", value: model.eval_mode ?? "missing", severity: model.eval_mode === "walk_forward" ? "pass" : "watch" },
    { label: "active", value: promotion?.active?.model_name ?? "none", severity: promotion?.active?.model_name === model.name ? "pass" : "watch" },
    { label: "shadow", value: promotion?.shadow?.model_name ?? "none", severity: promotion?.shadow?.model_name === model.name ? "pass" : "watch" },
  ];
}

function primaryAuc(model: ModelRecord): number | null {
  return model.cv_summary?.mean_auc ?? model.holdout_auc ?? null;
}

function stabilitySeverity(model: ModelRecord): ModelDossierSeverity {
  if (model.eval_mode !== "walk_forward") return "watch";
  if (!model.cv_summary) return "fail";
  const nScored = model.cv_summary.n_scored ?? 0;
  const nFolds = model.cv_summary.n_folds ?? 0;
  if (nScored <= 0) return "fail";
  if (nScored < Math.max(2, Math.floor(nFolds * 0.6))) return "watch";
  if (model.cv_summary.std_auc != null && model.cv_summary.std_auc > 0.05) return "watch";
  return "pass";
}

function stabilityDetail(model: ModelRecord): string {
  if (model.eval_mode !== "walk_forward") return "No walk-forward folds; rely on legacy holdout with caution.";
  if (!model.cv_summary) return "Walk-forward summary is missing.";
  const std = model.cv_summary.std_auc != null ? `, std ${model.cv_summary.std_auc.toFixed(3)}` : "";
  return `${model.cv_summary.n_scored ?? 0}/${model.cv_summary.n_folds ?? 0} folds scored${std}.`;
}

function promotionStateSeverity(
  modelName: string,
  active: ActiveBinding | null,
  shadow: ActiveBinding | null,
): ModelDossierSeverity {
  if (active?.model_name === modelName) return "pass";
  if (shadow?.model_name === modelName) return "watch";
  return "watch";
}

function promotionStateDetail(
  modelName: string,
  active: ActiveBinding | null,
  shadow: ActiveBinding | null,
): string {
  if (active?.model_name === modelName) return "This model is active for the agent.";
  if (shadow?.model_name === modelName) return "This model is shadow-bound; compare prediction logs before promotion.";
  if (active) return `Active model is ${active.model_name}; this candidate is not currently bound.`;
  return "No active model binding exists yet.";
}

function buildActions(state: ModelDossierState, checks: ModelDossierCheck[]): string[] {
  if (state === "ready") return ["Review live prediction behavior and promotion history before using Promote or Shadow."];
  return checks
    .filter((check) => check.severity !== "pass")
    .map((check) => `${check.label}: ${check.detail}`)
    .slice(0, 6);
}

function headlineFor(state: ModelDossierState, failed: number, watches: number): string {
  if (state === "blocked") return `${failed} hard blocker${failed === 1 ? "" : "s"} before model promotion review.`;
  if (state === "review") return `${watches} watch item${watches === 1 ? "" : "s"}; compare evidence before promotion.`;
  return "Model dossier is ready for promotion review.";
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
