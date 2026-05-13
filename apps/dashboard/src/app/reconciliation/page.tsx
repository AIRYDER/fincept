"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Database, GitCompareArrows, Loader2, Rocket, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { ReconChecklistPanel } from "@/components/reconciliation/recon-checklist-panel";
import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Position } from "@/lib/types";
import { cn, formatNumber, formatUsd } from "@/lib/utils";

function asNum(value: string | number | null | undefined) {
  if (value === null || value === undefined) return 0;
  const n = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(n) ? n : 0;
}

export default function ReconciliationPage() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const positionsQ = useQuery({
    queryKey: ["positions", "reconciliation"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const strategiesQ = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.strategies(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const configsQ = useQuery({
    queryKey: ["strategies", "configs"],
    queryFn: () => api.strategyConfigs(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const universeQ = useQuery({
    queryKey: ["universe"],
    queryFn: () => api.universe(token),
    enabled: !!token,
  });
  const coverageQ = useQuery({
    queryKey: ["data-coverage", "reconciliation"],
    queryFn: () => api.dataCoverage(token, { freq: "1m" }),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const ordersQ = useQuery({
    queryKey: ["orders", "reconciliation"],
    queryFn: () => api.orders(token, { limit: 100 }),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const adopt = useMutation({
    mutationFn: (strategyId: string) => api.adoptStrategyConfig(token, strategyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      queryClient.invalidateQueries({ queryKey: ["strategies", "configs"] });
    },
  });
  const seed = useMutation({
    mutationFn: () => api.seedUniverseFromPositions(token),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["universe"] });
      queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
    },
  });
  const startMarketData = useMutation({
    mutationFn: () => api.startFeature(token, "market_data"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["services"] });
      window.setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
      }, 2500);
    },
  });

  const report = useMemo(() => {
    const positions = positionsQ.data ?? [];
    const openPositions = positions.filter((p) => asNum(p.quantity) !== 0);
    const configsByStrategy = new Map((configsQ.data ?? []).map((row) => [row.strategy_id, row]));
    const runtimeByStrategy = new Map((strategiesQ.data ?? []).map((row) => [row.strategy_id, row]));
    const universeSymbols = new Set((universeQ.data ?? []).map((row) => row.symbol));
    const coverageBySymbol = new Map((coverageQ.data?.rows ?? []).map((row) => [row.symbol, row]));
    const grouped = new Map<string, Position[]>();
    for (const position of openPositions) {
      const rows = grouped.get(position.strategy_id) ?? [];
      rows.push(position);
      grouped.set(position.strategy_id, rows);
    }
    const strategies = Array.from(grouped.entries()).map(([strategyId, rows]) => {
      const config = configsByStrategy.get(strategyId) ?? null;
      const runtime = runtimeByStrategy.get(strategyId) ?? null;
      const missingUniverse = rows.filter((row) => !universeSymbols.has(row.symbol));
      const staleCoverage = rows.filter((row) => {
        const coverage = coverageBySymbol.get(row.symbol);
        return !coverage || coverage.status !== "ok";
      });
      const gross = rows.reduce((acc, row) => {
        const mark = asNum(row.mark_px) || asNum(row.avg_cost);
        return acc + Math.abs(asNum(row.quantity) * mark);
      }, 0);
      const unrealized = rows.reduce((acc, row) => acc + asNum(row.unrealized_pnl), 0);
      const issues = [
        !config ? "missing config" : null,
        config && !config.enabled ? "config disabled" : null,
        !runtime ? "no runtime row" : null,
        missingUniverse.length ? "missing universe" : null,
        staleCoverage.length ? "coverage gap" : null,
      ].filter(Boolean) as string[];
      return { strategyId, rows, config, runtime, missingUniverse, staleCoverage, gross, unrealized, issues };
    });
    const pendingOrders = (ordersQ.data ?? []).filter((order) => ["pending_new", "new", "partially_filled"].includes(order.status));
    const rejectedOrders = (ordersQ.data ?? []).filter((order) => order.status === "rejected");
    const issueCount = strategies.reduce((acc, row) => acc + row.issues.length, 0) + pendingOrders.length + rejectedOrders.length;
    return { strategies, openPositions, pendingOrders, rejectedOrders, issueCount };
  }, [configsQ.data, coverageQ.data, ordersQ.data, positionsQ.data, strategiesQ.data, universeQ.data]);

  return (
    <AppShell>
      <PageHeader
        title="State Reconciliation"
        description="Compare portfolio positions, strategy configs, runtime rows, universe membership, market-data coverage, and recent order state. Broker parity can plug in here when a broker positions endpoint is exposed."
        action={
          <div className="flex flex-wrap justify-end gap-2">
            <Badge variant={report.issueCount ? "warn" : "long"}>
              {report.issueCount ? `${report.issueCount} issues` : "Clean"}
            </Badge>
            <Button asChild variant="outline" size="sm">
              <Link href="/strategies">Strategies</Link>
            </Button>
          </div>
        }
      />

      <ReconChecklistPanel
        positions={positionsQ.data ?? []}
        strategies={strategiesQ.data ?? []}
        configs={configsQ.data ?? []}
        universe={universeQ.data ?? []}
        coverage={coverageQ.data?.rows ?? []}
        orders={ordersQ.data ?? []}
      />

      <div className="mb-4 grid gap-3 md:grid-cols-4">
        <ReconMetric label="Open positions" value={String(report.openPositions.length)} tone="cyan" />
        <ReconMetric label="Strategy groups" value={String(report.strategies.length)} tone="primary" />
        <ReconMetric label="Pending orders" value={String(report.pendingOrders.length)} tone={report.pendingOrders.length ? "warn" : "long"} />
        <ReconMetric label="Rejected orders" value={String(report.rejectedOrders.length)} tone={report.rejectedOrders.length ? "short" : "long"} />
      </div>

      <Card className="mb-4 border-primary/20">
        <CardHeader className="flex flex-row items-center justify-between gap-3 pb-3">
          <CardTitle className="flex items-center gap-2">
            <Rocket className="h-3.5 w-3.5 text-primary" />
            Repair actions
          </CardTitle>
          <Badge variant="muted">safe / no order placement</Badge>
        </CardHeader>
        <CardContent className="grid gap-2 md:grid-cols-3">
          <Button variant="outline" disabled={seed.isPending} onClick={() => seed.mutate()}>
            {seed.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}
            Seed universe from positions
          </Button>
          <Button variant="outline" disabled={startMarketData.isPending} onClick={() => startMarketData.mutate()}>
            {startMarketData.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Rocket className="h-3.5 w-3.5" />}
            Start market data
          </Button>
          <Button variant="outline" onClick={() => {
            queryClient.invalidateQueries({ queryKey: ["positions"] });
            queryClient.invalidateQueries({ queryKey: ["orders"] });
            queryClient.invalidateQueries({ queryKey: ["strategies"] });
            queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
          }}>
            <GitCompareArrows className="h-3.5 w-3.5" />
            Recheck state
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Strategy / position reconciliation</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="px-4 py-2 text-left">Strategy</th>
                <th className="px-4 py-2 text-right">Positions</th>
                <th className="px-4 py-2 text-right">Gross</th>
                <th className="px-4 py-2 text-right">Unrealized</th>
                <th className="px-4 py-2 text-left">Issues</th>
                <th className="px-4 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {report.strategies.map((row) => (
                <tr key={row.strategyId} className="border-b border-border/30 last:border-b-0 hover:bg-accent/30">
                  <td className="px-4 py-2 font-mono text-xs">{row.strategyId}</td>
                  <td className="num px-4 py-2 text-right">{row.rows.length}</td>
                  <td className="num px-4 py-2 text-right">{formatUsd(row.gross)}</td>
                  <td className={cn("num px-4 py-2 text-right", row.unrealized >= 0 ? "text-long" : "text-short")}>{formatUsd(row.unrealized, { signed: true })}</td>
                  <td className="px-4 py-2">
                    {row.issues.length ? (
                      <div className="flex flex-wrap gap-1">
                        {row.issues.map((issue) => <Badge key={issue} variant={issue.includes("missing") || issue.includes("gap") ? "warn" : "muted"}>{issue}</Badge>)}
                      </div>
                    ) : (
                      <Badge variant="long"><CheckCircle2 className="h-3 w-3" /> aligned</Badge>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {!row.config ? (
                      <Button size="sm" variant="outline" disabled={adopt.isPending} onClick={() => adopt.mutate(row.strategyId)}>
                        {adopt.isPending && adopt.variables === row.strategyId ? <Loader2 className="h-3 w-3 animate-spin" /> : <ShieldCheck className="h-3 w-3" />}
                        Adopt tracker
                      </Button>
                    ) : (
                      <Badge variant={row.config.enabled ? "long" : "muted"}>{row.config.enabled ? "enabled" : "disabled"}</Badge>
                    )}
                  </td>
                </tr>
              ))}
              {report.strategies.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">
                    No open positions to reconcile.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </AppShell>
  );
}

function ReconMetric({ label, value, tone }: { label: string; value: string; tone: "primary" | "cyan" | "long" | "warn" | "short" }) {
  const color = {
    primary: "text-primary",
    cyan: "text-cyan",
    long: "text-long",
    warn: "text-warn",
    short: "text-short",
  }[tone];
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
        <div className={cn("num mt-1 text-2xl font-semibold", color)}>{formatNumber(value, 0)}</div>
      </CardContent>
    </Card>
  );
}
