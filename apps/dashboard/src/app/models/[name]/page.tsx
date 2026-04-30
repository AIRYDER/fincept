"use client";

/**
 * /models/[name] — single-model detail.
 *
 * Surfaces the data the trainer wrote in ``meta.json``:
 *
 *   1. Headline metrics card  -- AUC mean/std + age + horizon.
 *   2. CV folds bar chart     -- per-fold val AUC; the mean line shows
 *                                stability (or lack thereof) at a glance.
 *   3. Training-config panel  -- purge bars, embargo, refit rounds —
 *                                everything you'd need to reproduce a run.
 *   4. Feature importance     -- horizontal bar of split counts (or gain
 *                                if the trainer wrote a sidecar).
 *
 * The page degrades gracefully:
 *   * legacy 80/20 holdout models hide the CV-folds chart and show a
 *     compact holdout panel instead.
 *   * malformed meta still renders identity + warnings.
 *   * 404 routes back to the listing with a helpful message.
 */

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowLeft,
  Brain,
  ChartBar,
  Clock,
  Database,
  HardDrive,
  Layers,
  Sigma,
  Target,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { LivePredictionsCard } from "@/components/models/live-predictions-card";
import { PromoteButton } from "@/components/models/promote-button";
import { ShadowButton } from "@/components/models/shadow-button";
import { AppShell } from "@/components/shell/app-shell";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  FeatureImportanceRow,
  ModelCvFold,
  ModelRecord,
} from "@/lib/types";

export default function ModelDetailPage() {
  const params = useParams<{ name: string }>();
  const name = decodeURIComponent(params?.name ?? "");
  const token = useAuth((s) => s.token);

  const detail = useQuery({
    queryKey: ["models", "detail", name],
    queryFn: () => api.modelDetail(token, name),
    enabled: !!token && !!name,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: (count, err) =>
      // Don't retry 404s — model genuinely doesn't exist.
      err instanceof ApiError && err.status === 404 ? false : count < 2,
  });

  const importance = useQuery({
    queryKey: ["models", "importance", name],
    queryFn: () => api.modelFeatureImportance(token, name),
    enabled: !!token && !!name && detail.data?.model_file_exists !== false,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: (count, err) =>
      err instanceof ApiError && err.status === 404 ? false : count < 2,
  });

  // Notably, importance can fail (e.g., model.txt missing) while detail
  // succeeds; render each section independently rather than coupling.

  if (detail.error instanceof ApiError && detail.error.status === 404) {
    return (
      <AppShell>
        <PageHeader
          title="Model not found"
          description={`No registered model named "${name}".`}
        />
        <Card>
          <CardContent className="py-10">
            <EmptyState
              icon={Database}
              title={`No model "${name}"`}
              description="The directory may have been deleted, or the name was mistyped."
            />
            <div className="mt-4 flex justify-center">
              <Link href="/models">
                <Button variant="outline" size="sm">
                  <ArrowLeft className="mr-2 h-3.5 w-3.5" />
                  Back to models
                </Button>
              </Link>
            </div>
          </CardContent>
        </Card>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <PageHeader
        title={
          <span className="flex items-center gap-3 font-mono">
            <Link
              href="/models"
              className="text-muted-foreground hover:text-foreground"
              aria-label="Back to models"
            >
              <ArrowLeft className="h-5 w-5" />
            </Link>
            {name}
          </span>
        }
        description={
          detail.data
            ? `${labelForEvalMode(detail.data.eval_mode)} · ${detail.data.feature_count} features · ${detail.data.horizon_bars ?? "?"}-bar horizon`
            : "Loading model metadata…"
        }
        action={
          detail.data ? (
            <div className="flex items-center gap-3">
              <EvalModeBadge eval_mode={detail.data.eval_mode} />
              {detail.data.model_file_exists ? (
                <>
                  <PromoteButton modelName={name} />
                  <ShadowButton modelName={name} />
                </>
              ) : null}
            </div>
          ) : undefined
        }
      />

      {/* Warnings, if any */}
      {detail.data?.warnings && detail.data.warnings.length > 0 ? (
        <Card className="border-warn/40 bg-warn/5">
          <CardContent className="flex items-start gap-3 py-3 text-sm">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
            <div>
              <div className="font-semibold text-warn">Warnings</div>
              <ul className="mt-0.5 list-disc pl-5 text-warn/90">
                {detail.data.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* Headline + meta */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_3fr]">
        <HeadlineCard m={detail.data} />
        <ConfigPanel m={detail.data} />
      </div>

      {/* CV folds chart (walk-forward only) */}
      {detail.data?.eval_mode === "walk_forward" ? (
        <Card className="mt-6">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <ChartBar className="h-4 w-4 text-primary" />
              Walk-forward CV folds
            </CardTitle>
            <CardDescription>
              Per-fold validation AUC.  The dashed line is the mean
              across scored folds; tightly clustered bars indicate
              regime-stable predictive signal, scattered bars indicate
              the strategy may be over-fitting individual periods.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <CvFoldsChart
              folds={detail.data?.cv_folds ?? []}
              meanAuc={detail.data?.cv_summary?.mean_auc ?? null}
            />
          </CardContent>
        </Card>
      ) : null}

      {/* Feature importance */}
      <Card className="mt-6">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Layers className="h-4 w-4 text-primary" />
            Feature importance
          </CardTitle>
          <CardDescription>
            {importance.data?.importance_type === "gain_and_split"
              ? "Gain-based importance — sum of split gains across all trees."
              : "Split-count importance — number of times each feature was used as a split.  Gain-based importance becomes available after the trainer writes a feature_importance.json sidecar."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FeatureImportancePanel
            data={importance.data}
            error={importance.error}
            modelFileMissing={detail.data?.model_file_exists === false}
          />
        </CardContent>
      </Card>

      {/* Live predictions (Phase D2) */}
      <LivePredictionsCard modelName={name} />
    </AppShell>
  );
}

// --------------------------------------------------------------------------- //
// Subcomponents                                                              //
// --------------------------------------------------------------------------- //

function EvalModeBadge({ eval_mode }: { eval_mode: string | null }) {
  if (eval_mode === "walk_forward") {
    return (
      <Badge
        variant="long"
        className="font-mono uppercase tracking-widest"
      >
        Walk-forward
      </Badge>
    );
  }
  if (eval_mode === "holdout_80_20") {
    return (
      <Badge
        variant="warn"
        className="font-mono uppercase tracking-widest"
      >
        80/20 holdout
      </Badge>
    );
  }
  return (
    <Badge variant="muted" className="font-mono uppercase tracking-widest">
      Unknown
    </Badge>
  );
}

function HeadlineCard({ m }: { m: ModelRecord | undefined }) {
  if (!m) {
    return (
      <Card>
        <CardContent className="py-10">
          <EmptyState
            icon={Brain}
            title="Loading…"
            description="Fetching model metadata."
          />
        </CardContent>
      </Card>
    );
  }
  const mean = m.cv_summary?.mean_auc ?? m.holdout_auc ?? null;
  const std = m.cv_summary?.std_auc ?? null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Sigma className="h-4 w-4 text-primary" />
          Predictive quality
        </CardTitle>
        <CardDescription>
          Higher AUC ⇒ better separation of up vs down moves at the
          horizon.  0.5 = coin-flip, 1.0 = perfect.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <div className="flex items-baseline gap-3">
            <span className="font-mono text-5xl font-bold">
              {mean != null ? mean.toFixed(3) : "—"}
            </span>
            <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
              {m.eval_mode === "walk_forward" ? "mean AUC" : "holdout AUC"}
            </span>
          </div>
          {std != null ? (
            <div className="text-xs text-muted-foreground">
              ± {std.toFixed(3)} across {m.cv_summary?.n_scored ?? 0} folds
              {m.cv_summary?.n_skipped
                ? ` (${m.cv_summary.n_skipped} skipped)`
                : ""}
            </div>
          ) : m.holdout_rows ? (
            <div className="text-xs text-muted-foreground">
              {m.holdout_rows.toLocaleString()} validation rows
            </div>
          ) : null}
        </div>

        <div className="grid grid-cols-3 gap-2">
          <Stat
            icon={Target}
            label="Horizon"
            value={
              m.horizon_bars != null
                ? `${m.horizon_bars} bars`
                : "—"
            }
            sub={
              m.bar_seconds != null && m.horizon_bars != null
                ? `≈ ${formatHorizonHuman(m.horizon_bars * m.bar_seconds)}`
                : undefined
            }
          />
          <Stat
            icon={Clock}
            label="Trained"
            value={formatAge(m.age_seconds)}
          />
          <Stat
            icon={Database}
            label="Features"
            value={String(m.feature_count)}
          />
        </div>

        {!m.model_file_exists ? (
          <div className="flex items-center gap-2 rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            <HardDrive className="h-3.5 w-3.5" />
            <span>
              <code className="font-mono">model.txt</code> missing — the
              binary booster file is gone, so this model can't be loaded
              for inference.  Re-train to regenerate.
            </span>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Stat({
  icon: Icon,
  label,
  value,
  sub,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="rounded-md border border-border/40 bg-background/30 p-3">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <div className="mt-1 font-mono text-base font-semibold">{value}</div>
      {sub ? (
        <div className="text-[10px] text-muted-foreground">{sub}</div>
      ) : null}
    </div>
  );
}

function ConfigPanel({ m }: { m: ModelRecord | undefined }) {
  if (!m) {
    return (
      <Card>
        <CardContent className="py-10">
          <EmptyState
            icon={Brain}
            title="Loading…"
            description="Fetching training config."
          />
        </CardContent>
      </Card>
    );
  }
  const rows: Array<[string, string]> = [];
  if (m.eval_mode === "walk_forward") {
    rows.push([
      "Folds",
      `${m.cv_summary?.n_folds ?? "?"} (${m.cv_summary?.n_scored ?? "?"} scored)`,
    ]);
    if (m.purge_bars != null) rows.push(["Purge bars", String(m.purge_bars)]);
    if (m.embargo_bars != null)
      rows.push(["Embargo bars", String(m.embargo_bars)]);
    if (m.cv_summary?.median_best_iter != null)
      rows.push(["Median best_iter", String(m.cv_summary.median_best_iter)]);
    if (m.final_train_rows != null)
      rows.push(["Final train rows", m.final_train_rows.toLocaleString()]);
    if (m.final_num_boost_round != null)
      rows.push(["Final boost rounds", String(m.final_num_boost_round)]);
  } else if (m.eval_mode === "holdout_80_20") {
    if (m.holdout_rows != null)
      rows.push(["Validation rows", m.holdout_rows.toLocaleString()]);
  }
  if (m.bar_seconds != null) rows.push(["Bar duration", `${m.bar_seconds}s`]);
  if (m.horizon_ns != null)
    rows.push(["Horizon (ns)", m.horizon_ns.toLocaleString()]);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <Brain className="h-4 w-4 text-primary" />
          Training config
        </CardTitle>
        <CardDescription>
          Reproducibility details written by the trainer at save time.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
          {rows.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              No training config recorded — this model was likely
              trained before the meta schema landed.
            </div>
          ) : (
            rows.map(([label, value]) => (
              <div
                key={label}
                className="flex items-baseline justify-between gap-3 border-b border-border/30 py-1"
              >
                <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
                  {label}
                </span>
                <span className="font-mono text-xs">{value}</span>
              </div>
            ))
          )}
        </div>
        <div className="mt-3 text-[10px] uppercase tracking-widest text-muted-foreground">
          Path
        </div>
        <code className="mt-1 block break-all rounded bg-background/40 p-2 font-mono text-[11px]">
          {m.path}
        </code>
      </CardContent>
    </Card>
  );
}

function CvFoldsChart({
  folds,
  meanAuc,
}: {
  folds: ModelCvFold[];
  meanAuc: number | null;
}) {
  if (folds.length === 0) {
    return (
      <EmptyState
        icon={ChartBar}
        title="No fold data"
        description="The trainer didn't record per-fold metrics."
      />
    );
  }
  const data = folds.map((f) => ({
    fold: `F${f.fold ?? "?"}`,
    auc: f.best_auc,
    skipped: f.reason_skipped != null,
    valRows: f.val_rows,
    bestIter: f.best_iter,
  }));
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3 }}
      className="h-72 w-full"
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data}>
          <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="2 4" />
          <XAxis
            dataKey="fold"
            fontSize={10}
            tick={{ fill: "hsl(var(--muted-foreground))" }}
            axisLine={{ stroke: "hsl(var(--border))" }}
            tickLine={false}
          />
          <YAxis
            domain={[0.4, 0.7]}
            fontSize={10}
            tick={{ fill: "hsl(var(--muted-foreground))" }}
            tickLine={false}
            axisLine={{ stroke: "hsl(var(--border))" }}
            tickFormatter={(v) => v.toFixed(2)}
          />
          <Tooltip
            contentStyle={{
              background: "hsl(var(--card))",
              border: "1px solid hsl(var(--border))",
              borderRadius: 6,
              fontSize: 12,
            }}
            // Recharts types the value as ValueType (string | number |
            // array); narrow at the boundary instead of arguing with
            // the generic.
            formatter={(value, _name, item) => {
              const v = typeof value === "number" ? value : null;
              if (v == null) return ["—", "AUC"];
              const valRows = item.payload.valRows ?? "—";
              const bestIter = item.payload.bestIter ?? "—";
              const valLabel =
                typeof valRows === "number" ? valRows.toLocaleString() : valRows;
              return [
                `${v.toFixed(4)} (val=${valLabel}, iter=${bestIter})`,
                "AUC",
              ];
            }}
          />
          {meanAuc != null ? (
            <ReferenceLine
              y={meanAuc}
              stroke="hsl(var(--primary))"
              strokeDasharray="3 3"
              label={{
                value: `mean ${meanAuc.toFixed(3)}`,
                fontSize: 10,
                fill: "hsl(var(--primary))",
                position: "top",
              }}
            />
          ) : null}
          <ReferenceLine
            y={0.5}
            stroke="hsl(var(--muted-foreground))"
            strokeDasharray="4 4"
          />
          <Bar dataKey="auc" radius={[2, 2, 0, 0]}>
            {data.map((d, i) => (
              <Cell
                key={i}
                fill={
                  d.skipped
                    ? "hsl(var(--muted-foreground) / 0.4)"
                    : d.auc != null && d.auc >= 0.5
                      ? "hsl(var(--long))"
                      : "hsl(var(--destructive))"
                }
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </motion.div>
  );
}

function FeatureImportancePanel({
  data,
  error,
  modelFileMissing,
}: {
  data:
    | {
        importances: FeatureImportanceRow[];
        importance_type: "split_count" | "gain_and_split";
        source: "model_text" | "sidecar";
        warnings: string[];
      }
    | undefined;
  error: unknown;
  modelFileMissing: boolean;
}) {
  if (modelFileMissing) {
    return (
      <EmptyState
        icon={HardDrive}
        title="No booster file"
        description="model.txt is missing — feature importance can't be computed without the trained model."
      />
    );
  }
  if (error) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="Importance unavailable"
        description={
          error instanceof Error ? error.message : "Unknown error"
        }
      />
    );
  }
  if (!data) {
    return (
      <EmptyState
        icon={Layers}
        title="Loading importance…"
        description="Parsing the booster file."
      />
    );
  }
  const rows = data.importances;
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Layers}
        title="No importance data"
        description="The model has no recorded splits — likely an empty or untrained booster."
      />
    );
  }
  const useGain = data.importance_type === "gain_and_split";
  const dataKey = useGain ? "gain" : "split_count";
  const chartData = rows.map((r) => ({
    feature: r.feature,
    split_count: r.split_count,
    gain: r.gain ?? 0,
    rank: r.rank,
  }));
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3 }}
      className="space-y-2"
    >
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-muted-foreground">
        <span>source: {data.source}</span>
        <span>·</span>
        <span>type: {data.importance_type.replace("_", " ")}</span>
      </div>
      <div style={{ height: Math.max(220, rows.length * 28 + 40) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            layout="vertical"
            margin={{ top: 4, right: 24, bottom: 4, left: 12 }}
          >
            <CartesianGrid
              stroke="hsl(var(--border))"
              strokeDasharray="2 4"
              horizontal={false}
            />
            <XAxis
              type="number"
              fontSize={10}
              tick={{ fill: "hsl(var(--muted-foreground))" }}
              tickLine={false}
              axisLine={{ stroke: "hsl(var(--border))" }}
            />
            <YAxis
              type="category"
              dataKey="feature"
              fontSize={11}
              tick={{ fill: "hsl(var(--foreground))" }}
              tickLine={false}
              axisLine={{ stroke: "hsl(var(--border))" }}
              width={120}
            />
            <Tooltip
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                borderRadius: 6,
                fontSize: 12,
              }}
              formatter={(value, _name, item) => {
                const v = typeof value === "number" ? value : Number(value);
                if (useGain) {
                  return [
                    `${v.toFixed(2)}  (splits=${item.payload.split_count})`,
                    "gain",
                  ];
                }
                return [String(v), "splits"];
              }}
            />
            <Bar
              dataKey={dataKey}
              fill="hsl(var(--primary))"
              radius={[0, 2, 2, 0]}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
      {data.warnings.length > 0 ? (
        <div className="rounded border border-warn/40 bg-warn/5 px-2 py-1 text-[11px] text-warn">
          {data.warnings.join("; ")}
        </div>
      ) : null}
    </motion.div>
  );
}

function labelForEvalMode(mode: string | null): string {
  if (mode === "walk_forward") return "Walk-forward CV";
  if (mode === "holdout_80_20") return "Legacy 80/20 holdout";
  return "Unknown evaluation mode";
}

function formatAge(seconds: number | null): string {
  if (seconds == null) return "unknown";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function formatHorizonHuman(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  if (totalSeconds < 3600) return `${Math.round(totalSeconds / 60)}m`;
  if (totalSeconds < 86400)
    return `${(totalSeconds / 3600).toFixed(1)}h`;
  return `${(totalSeconds / 86400).toFixed(1)}d`;
}
