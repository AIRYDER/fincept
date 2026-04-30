"use client";

/**
 * /models — model registry overview.
 *
 * Lists every directory under MODELS_DIR with a meta.json, grouped into
 * walk-forward and legacy holdout cohorts.  Cards link through to the
 * detail page (CV folds + feature importance).  This is the "Phase A"
 * read-only view; subsequent phases add a Train dialog and a deployment
 * action on the detail page.
 *
 * Polling cadence is intentionally relaxed (60s) — models change only
 * on retrain, and a stale age badge is more useful than a tight loop.
 */

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  Brain,
  CheckCircle2,
  Database,
  HardDrive,
} from "lucide-react";
import Link from "next/link";

import { PromotionHistoryPanel } from "@/components/models/promotion-history-panel";
import { RunsPanel } from "@/components/models/runs-panel";
import { TrainModelDialog } from "@/components/models/train-model-dialog";
import { AppShell } from "@/components/shell/app-shell";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ModelRecord } from "@/lib/types";
import { cn } from "@/lib/utils";

export default function ModelsPage() {
  const token = useAuth((s) => s.token);

  const { data, isLoading, error } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.models(token),
    enabled: !!token,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  // The active-model state is small and changes only on a deliberate
  // promote/rollback, so a relaxed cadence is fine.  We don't gate the
  // page on its loading state -- the badge just appears when the data
  // arrives.
  const promotion = useQuery({
    queryKey: ["models", "promote", "gbm_predictor.v1"],
    queryFn: () =>
      api.modelPromotionState(token, {
        agent_id: "gbm_predictor.v1",
        history_limit: 1,
      }),
    enabled: !!token,
    staleTime: 30_000,
  });

  const models = data?.models ?? [];
  const summary = data?.summary;
  const activeModelName = promotion.data?.active?.model_name ?? null;

  return (
    <AppShell>
      <PageHeader
        title="Models"
        description="Trained models with their evaluation provenance.  Click any card to drill into per-fold CV, feature importance, and the training config that produced it."
        action={
          <div className="flex items-center gap-3">
            {summary ? (
              <Badge variant="muted" className="font-mono">
                {summary.count} model{summary.count === 1 ? "" : "s"} ·{" "}
                {summary.with_cv} CV / {summary.with_holdout} holdout
              </Badge>
            ) : null}
            <TrainModelDialog />
          </div>
        }
      />

      {/* Summary tiles */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <SummaryTile
          icon={Database}
          label="Total models"
          value={summary?.count ?? 0}
        />
        <SummaryTile
          icon={CheckCircle2}
          label="Walk-forward CV"
          value={summary?.with_cv ?? 0}
          variant="success"
        />
        <SummaryTile
          icon={Brain}
          label="Legacy 80/20 holdout"
          value={summary?.with_holdout ?? 0}
          variant="warn"
        />
        <SummaryTile
          icon={AlertTriangle}
          label="With warnings"
          value={summary?.with_warnings ?? 0}
          variant={
            summary && summary.with_warnings > 0 ? "danger" : "muted"
          }
        />
      </div>

      {/* Models grid */}
      <Card className="mt-6">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Brain className="h-4 w-4 text-primary" />
            Registered models
          </CardTitle>
          <CardDescription>
            Reading from{" "}
            <code className="font-mono text-[11px]">
              {summary?.models_dir ?? "models/"}
            </code>
            .  Mean ± std AUC across folds is shown for walk-forward models;
            single-split AUC for legacy holdout models.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error ? (
            <EmptyState
              icon={AlertTriangle}
              title="Failed to load models"
              description={
                error instanceof Error ? error.message : "Unknown error"
              }
            />
          ) : isLoading ? (
            <EmptyState
              icon={Brain}
              title="Loading models..."
              description="Polling /models every 60s."
            />
          ) : models.length === 0 ? (
            <EmptyState
              icon={Database}
              title="No models found"
              description={`Train one with: python -m agents.gbm_predictor.train --input data/X.parquet --cv-folds 5 --out-dir ${summary?.models_dir ?? "models/gbm_predictor"}`}
            />
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {models.map((m, i) => (
                <ModelCard
                  key={m.name}
                  m={m}
                  index={i}
                  isActive={activeModelName === m.name}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <PromotionHistoryPanel />

      <RunsPanel />
    </AppShell>
  );
}

// --------------------------------------------------------------------------- //
// Subcomponents                                                              //
// --------------------------------------------------------------------------- //

function SummaryTile({
  icon: Icon,
  label,
  value,
  variant = "default",
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number;
  variant?: "default" | "success" | "warn" | "danger" | "muted";
}) {
  const tone = {
    default: "border-border/40 bg-background/30 text-foreground",
    success: "border-long/30 bg-long/5 text-long",
    warn: "border-warn/30 bg-warn/5 text-warn",
    danger: "border-destructive/30 bg-destructive/5 text-destructive",
    muted: "border-border/40 bg-background/30 text-muted-foreground",
  }[variant];
  return (
    <div className={cn("rounded-md border p-3 transition-colors", tone)}>
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-widest">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="mt-1 font-mono text-2xl font-bold">{value}</div>
    </div>
  );
}

function ModelCard({
  m,
  index,
  isActive,
}: {
  m: ModelRecord;
  index: number;
  isActive: boolean;
}) {
  const ageLabel = formatAge(m.age_seconds);
  const evalBadge =
    m.eval_mode === "walk_forward"
      ? { label: "WALK-FWD", classes: "text-long border-long/40 bg-long/5" }
      : m.eval_mode === "holdout_80_20"
        ? {
            label: "80/20 HOLDOUT",
            classes: "text-warn border-warn/40 bg-warn/5",
          }
        : {
            label: "UNKNOWN",
            classes:
              "text-muted-foreground border-border/60 bg-muted/5",
          };
  const aucDisplay = (() => {
    if (m.cv_summary?.mean_auc != null) {
      const mean = m.cv_summary.mean_auc;
      const std = m.cv_summary.std_auc ?? 0;
      return {
        primary: mean.toFixed(3),
        secondary: `± ${std.toFixed(3)} across ${m.cv_summary.n_scored ?? 0} folds`,
      };
    }
    if (m.holdout_auc != null) {
      return {
        primary: m.holdout_auc.toFixed(3),
        secondary: `single 80/20 holdout (${m.holdout_rows ?? "?"} val rows)`,
      };
    }
    return { primary: "—", secondary: "no AUC recorded" };
  })();

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.03, 0.2) }}
    >
      <Link
        href={`/models/${encodeURIComponent(m.name)}`}
        className="group block focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 rounded-md"
      >
        <div
          className={cn(
            "flex h-full flex-col gap-2 rounded-md border bg-background/30 p-4 transition-colors hover:bg-accent/40",
            isActive
              ? "border-long/50 hover:border-long/70 ring-1 ring-long/20"
              : "border-border/40 hover:border-primary/40",
          )}
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate font-mono text-sm font-semibold group-hover:text-primary">
                {m.name}
              </span>
              {isActive ? (
                <span
                  className="shrink-0 rounded border border-long/40 bg-long/10 px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-widest text-long"
                  title="Active model -- restart the agent to actually load it"
                >
                  Active
                </span>
              ) : null}
            </div>
            <span
              className={cn(
                "shrink-0 rounded border px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest",
                evalBadge.classes,
              )}
            >
              {evalBadge.label}
            </span>
          </div>
          <div>
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-2xl font-bold">
                {aucDisplay.primary}
              </span>
              <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
                AUC
              </span>
            </div>
            <div className="text-xs text-muted-foreground">
              {aucDisplay.secondary}
            </div>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] font-mono uppercase tracking-widest text-muted-foreground">
            <span>{m.feature_count} features</span>
            {m.horizon_bars != null ? (
              <span>{m.horizon_bars}-bar horizon</span>
            ) : null}
            <span>trained {ageLabel}</span>
          </div>
          {m.warnings.length > 0 ? (
            <div className="rounded border border-warn/40 bg-warn/5 px-2 py-1 text-[11px] text-warn">
              {m.warnings.join("; ")}
            </div>
          ) : null}
          {!m.model_file_exists ? (
            <div className="flex items-center gap-1 text-[11px] text-destructive">
              <HardDrive className="h-3 w-3" />
              model.txt missing — inference disabled
            </div>
          ) : null}
        </div>
      </Link>
    </motion.div>
  );
}

function formatAge(seconds: number | null): string {
  if (seconds == null) return "unknown";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
