"use client";

import {
  AlertTriangle,
  CheckCircle2,
  GitBranch,
  LineChart,
  SlidersHorizontal,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

import type { PortfolioAllocationResult } from "./portfolioBuilder.types";

const METHOD_LABELS: Record<string, string> = {
  heuristic: "Heuristic allocator",
  inverse_volatility: "Inverse volatility",
  risk_parity: "Risk parity",
  mean_variance: "Mean variance",
  black_litterman: "Black-Litterman",
  cvar_min_drawdown: "CVaR / min drawdown",
};

export function OptimizerControlTower({
  allocation,
}: {
  allocation: PortfolioAllocationResult;
}) {
  const diagnostics = allocation.optimization;
  const feasibleFrontier = (diagnostics.frontier ?? []).filter(
    (point) => point.feasible,
  );
  const lowestVolPoint = feasibleFrontier.reduce(
    (best, point) =>
      !best || point.annualVolatilityPct < best.annualVolatilityPct
        ? point
        : best,
    feasibleFrontier[0],
  );
  const highestReturnPoint = feasibleFrontier.reduce(
    (best, point) =>
      !best || point.expectedReturnPct > best.expectedReturnPct
        ? point
        : best,
    feasibleFrontier[0],
  );
  const warnings = [...allocation.warnings, ...diagnostics.warnings];
  const reviewState =
    warnings.length > 0 || diagnostics.bindingConstraints.length > 0
      ? "Review"
      : "Clean";

  return (
    <Card className="border-cyan/30">
      <CardHeader className="flex-row items-center justify-between gap-3">
        <CardTitle>
          <SlidersHorizontal className="h-3.5 w-3.5 text-cyan" />
          Optimizer Control Tower
        </CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={diagnostics.feasible ? "default" : "destructive"}>
            {diagnostics.feasible ? "Feasible" : "Infeasible"}
          </Badge>
          <Badge variant={reviewState === "Clean" ? "muted" : "warn"}>
            {reviewState}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
        <section className="space-y-3">
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Metric
              label="Expected return"
              value={signedPct(diagnostics.expectedReturnPct)}
              tone={diagnostics.expectedReturnPct >= 0 ? "text-long" : "text-short"}
            />
            <Metric
              label="Annual volatility"
              value={pct(diagnostics.annualVolatilityPct)}
            />
            <Metric
              label="CVaR proxy"
              value={pct(diagnostics.cvarProxyPct)}
              tone="text-warn"
            />
            <Metric
              label="Sharpe-like"
              value={diagnostics.sharpeLikeScore.toFixed(2)}
              tone={diagnostics.sharpeLikeScore >= 0.5 ? "text-long" : "text-cyan"}
            />
          </div>

          <div className="grid gap-3 lg:grid-cols-[0.85fr_1.15fr]">
            <div className="border border-border p-3">
              <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                <CheckCircle2 className="h-3 w-3 text-cyan" />
                Solver packet
              </div>
              <dl className="grid gap-2 text-xs">
                <Row
                  label="Method"
                  value={METHOD_LABELS[diagnostics.method] ?? diagnostics.method}
                />
                <Row label="Iterations" value={String(diagnostics.iterations)} />
                <Row
                  label="Objective"
                  value={diagnostics.objectiveScore.toFixed(2)}
                />
                <Row
                  label="Drawdown proxy"
                  value={pct(diagnostics.maxDrawdownProxyPct)}
                />
              </dl>
            </div>

            <div className="border border-border p-3">
              <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                <GitBranch className="h-3 w-3 text-cyan" />
                Constraint pressure
              </div>
              {diagnostics.bindingConstraints.length ? (
                <ul className="space-y-1 text-xs leading-5 text-muted-foreground">
                  {diagnostics.bindingConstraints.slice(0, 5).map((constraint) => (
                    <li key={constraint} className="flex gap-2">
                      <span className="mt-2 h-1 w-1 shrink-0 bg-warn" />
                      <span>{constraint}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs leading-5 text-muted-foreground">
                  No binding constraints were detected after target holdings,
                  sector caps, intentional cash-reserve, and max-holding limits were applied.
                </p>
              )}
            </div>
          </div>

          {warnings.length ? (
            <div className="flex gap-2 border border-warn/40 bg-warn/5 p-2 text-[11px] leading-5 text-warn">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{warnings.slice(0, 3).join(" ")}</span>
            </div>
          ) : null}
        </section>

        <section className="border border-border p-3">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
              <LineChart className="h-3 w-3 text-cyan" />
              Efficient frontier explorer
            </div>
            <Badge variant="muted">{feasibleFrontier.length} feasible points</Badge>
          </div>

          <FrontierPlot allocation={allocation} />

          <div className="mt-3 grid gap-2 text-xs md:grid-cols-2">
            <MiniPoint
              label="Lowest volatility"
              value={
                lowestVolPoint
                  ? `${pct(lowestVolPoint.annualVolatilityPct)} vol / ${signedPct(lowestVolPoint.expectedReturnPct)} return`
                  : "No feasible point"
              }
            />
            <MiniPoint
              label="Highest return"
              value={
                highestReturnPoint
                  ? `${signedPct(highestReturnPoint.expectedReturnPct)} return / ${pct(highestReturnPoint.annualVolatilityPct)} vol`
                  : "No feasible point"
              }
            />
          </div>
        </section>
      </CardContent>
    </Card>
  );
}

function FrontierPlot({
  allocation,
}: {
  allocation: PortfolioAllocationResult;
}) {
  const diagnostics = allocation.optimization;
  const points = (diagnostics.frontier ?? []).slice(0, 18);
  const maxVol = Math.max(
    diagnostics.annualVolatilityPct,
    ...points.map((point) => point.annualVolatilityPct),
    1,
  );
  const minReturn = Math.min(
    diagnostics.expectedReturnPct,
    ...points.map((point) => point.expectedReturnPct),
  );
  const maxReturn = Math.max(
    diagnostics.expectedReturnPct,
    ...points.map((point) => point.expectedReturnPct),
    minReturn + 1,
  );

  if (!points.length) {
    return (
      <div className="flex h-40 items-center justify-center border border-dashed border-border text-xs text-muted-foreground">
        Frontier diagnostics will appear once the optimizer emits target-return
        points.
      </div>
    );
  }

  return (
    <div className="relative h-44 overflow-hidden border border-border bg-background/40">
      <div className="absolute inset-x-0 bottom-8 border-t border-border/60" />
      <div className="absolute inset-y-0 left-10 border-l border-border/60" />
      {points.map((point, index) => {
        const left = 10 + clamp((point.annualVolatilityPct / maxVol) * 86, 0, 86);
        const bottom =
          16 +
          clamp(
            ((point.expectedReturnPct - minReturn) / (maxReturn - minReturn)) *
              72,
            0,
            72,
          );
        return (
          <span
            key={`${point.targetReturnPct}-${index}`}
            className={cn(
              "absolute h-2.5 w-2.5 -translate-x-1/2 translate-y-1/2 border",
              point.feasible
                ? "border-cyan bg-cyan/70"
                : "border-border bg-muted/40",
            )}
            style={{ left: `${left}%`, bottom: `${bottom}%` }}
            title={`${signedPct(point.expectedReturnPct)} return / ${pct(point.annualVolatilityPct)} vol`}
          />
        );
      })}
      <span
        className="absolute h-3.5 w-3.5 -translate-x-1/2 translate-y-1/2 border border-primary bg-primary shadow-[0_0_14px_rgba(249,115,22,0.45)]"
        style={{
          left: `${10 + clamp((diagnostics.annualVolatilityPct / maxVol) * 86, 0, 86)}%`,
          bottom: `${
            16 +
            clamp(
              ((diagnostics.expectedReturnPct - minReturn) /
                (maxReturn - minReturn)) *
                72,
              0,
              72,
            )
          }%`,
        }}
        title="Current allocation"
      />
      <div className="absolute bottom-2 left-3 text-[10px] uppercase tracking-wider text-muted-foreground">
        Volatility
      </div>
      <div className="absolute left-3 top-2 text-[10px] uppercase tracking-wider text-muted-foreground">
        Return
      </div>
      <div className="absolute right-3 top-2 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span className="h-2 w-2 bg-primary" />
        Current
        <span className="h-2 w-2 bg-cyan/70" />
        Frontier
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <div className="border border-border p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-1 font-mono text-lg text-foreground", tone)}>
        {value}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/50 pb-1 last:border-0 last:pb-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono text-foreground">{value}</dd>
    </div>
  );
}

function MiniPoint({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-border/70 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 font-mono text-[11px] text-foreground">{value}</div>
    </div>
  );
}

function pct(value: number): string {
  return `${value.toFixed(2)}%`;
}

function signedPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
