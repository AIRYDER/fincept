"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  FlaskConical,
  History,
  Play,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

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
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  BacktestEquityPoint,
  BacktestManifest,
  BacktestPerSymbolStats,
  BacktestReport,
  BacktestRunResponse,
} from "@/lib/types";
import { cn, formatUsd } from "@/lib/utils";

const DEFAULT_STRATEGY_PARAMS: Record<string, string> = {
  buy_and_hold: '{"per_symbol_notional": 10000}',
  ma_crossover: '{"fast": 5, "slow": 30, "per_symbol_notional": 10000}',
};

const FREQ_OPTIONS = ["1m", "5m", "15m", "1h", "1d"] as const;

export default function BacktestPage() {
  const token = useAuth((s) => s.token);

  // ---------------------------------------------------------------- form state
  const [barsPath, setBarsPath] = useState("data/synth_ohlcv.parquet");
  const [strategyKey, setStrategyKey] = useState<string>("ma_crossover");
  const [paramsRaw, setParamsRaw] = useState<string>(
    DEFAULT_STRATEGY_PARAMS.ma_crossover,
  );
  const [startingCash, setStartingCash] = useState<string>("100000");
  const [freq, setFreq] = useState<(typeof FREQ_OPTIONS)[number]>("1m");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [paramError, setParamError] = useState<string | null>(null);

  // ---------------------------------------------------------------- queries
  const strategiesQuery = useQuery({
    queryKey: ["backtest", "strategies"],
    queryFn: () => api.backtestStrategies(token),
    enabled: !!token,
  });

  const runsQuery = useQuery({
    queryKey: ["backtest", "runs"],
    queryFn: () => api.backtestRuns(token),
    enabled: !!token,
    refetchInterval: 15_000,
  });

  const detailQuery = useQuery({
    queryKey: ["backtest", "run", activeRunId],
    queryFn: () => api.backtestRun(token, activeRunId!),
    enabled: !!token && !!activeRunId,
  });

  // ---------------------------------------------------------------- run mutation
  const runMutation = useMutation<BacktestRunResponse, ApiError>({
    mutationFn: () => {
      let parsedParams: Record<string, unknown> = {};
      if (paramsRaw.trim().length > 0) {
        try {
          parsedParams = JSON.parse(paramsRaw);
        } catch (exc) {
          throw new ApiError(
            400,
            { detail: `strategy_params is not valid JSON: ${String(exc)}` },
            "Invalid strategy_params JSON",
          );
        }
      }
      const cash = Number(startingCash);
      if (!Number.isFinite(cash) || cash <= 0) {
        throw new ApiError(
          400,
          { detail: "starting_cash must be a positive number" },
          "Invalid starting cash",
        );
      }
      return api.runBacktest(token, {
        bars_path: barsPath,
        strategy: strategyKey,
        strategy_params: parsedParams,
        starting_cash: cash,
        freq,
      });
    },
    onSuccess: (data) => {
      setActiveRunId(data.run_id);
      setParamError(null);
      runsQuery.refetch();
    },
    onError: (err) => {
      const detail =
        (err.body as { detail?: string } | null)?.detail ?? err.message;
      setParamError(detail);
    },
  });

  // Switch defaults when strategy dropdown changes (only if user hasn't customized).
  useEffect(() => {
    const def = DEFAULT_STRATEGY_PARAMS[strategyKey];
    if (def && Object.values(DEFAULT_STRATEGY_PARAMS).includes(paramsRaw)) {
      setParamsRaw(def);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyKey]);

  // Auto-select most recent run if nothing selected.
  useEffect(() => {
    if (!activeRunId && runsQuery.data?.runs?.length) {
      setActiveRunId(runsQuery.data.runs[0].run_id);
    }
  }, [activeRunId, runsQuery.data]);

  const detail = detailQuery.data;
  const report = detail?.report;
  const manifest = detail?.manifest;

  return (
    <AppShell>
      <div className="flex h-full flex-col gap-4 p-6">
        <PageHeader
          title="Backtest"
          description="Replay strategies against historical bars without burning paper capital. Results persist under reports/backtests/."
        />

        {/* Run form */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Play className="h-4 w-4 text-primary" />
              Run a backtest
            </CardTitle>
            <CardDescription>
              Provide a parquet with columns:{" "}
              <code className="font-mono text-[11px]">
                symbol, ts_event, open, high, low, close, volume
              </code>
              .  Generate one with{" "}
              <code className="font-mono text-[11px]">
                python scripts/build_synth_ohlcv.py
              </code>
              .
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
              <FormField label="Bars parquet (path)">
                <Input
                  value={barsPath}
                  onChange={(e) => setBarsPath(e.target.value)}
                  placeholder="data/synth_ohlcv.parquet"
                />
              </FormField>
              <FormField label="Strategy">
                <select
                  value={strategyKey}
                  onChange={(e) => setStrategyKey(e.target.value)}
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                >
                  {(strategiesQuery.data?.strategies ?? []).map((s) => (
                    <option key={s.key} value={s.key}>
                      {s.key}
                    </option>
                  ))}
                </select>
              </FormField>
              <FormField label="Starting cash (USD)">
                <Input
                  value={startingCash}
                  onChange={(e) => setStartingCash(e.target.value)}
                  inputMode="decimal"
                  placeholder="100000"
                />
              </FormField>
              <FormField label="Bar frequency">
                <select
                  value={freq}
                  onChange={(e) =>
                    setFreq(e.target.value as (typeof FREQ_OPTIONS)[number])
                  }
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                >
                  {FREQ_OPTIONS.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>
              </FormField>
            </div>
            <div className="mt-3">
              <FormField
                label="Strategy params (JSON)"
                hint={
                  strategiesQuery.data?.strategies.find(
                    (s) => s.key === strategyKey,
                  )?.description
                }
              >
                <textarea
                  value={paramsRaw}
                  onChange={(e) => setParamsRaw(e.target.value)}
                  rows={2}
                  className="w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs focus:outline-none focus:ring-1 focus:ring-primary"
                  placeholder='{"per_symbol_notional": 10000}'
                />
              </FormField>
            </div>
            <div className="mt-3 flex items-center gap-3">
              <Button
                onClick={() => runMutation.mutate()}
                disabled={runMutation.isPending || !token}
              >
                <Play className="mr-2 h-3.5 w-3.5" />
                {runMutation.isPending ? "Running..." : "Run backtest"}
              </Button>
              {paramError ? (
                <span className="flex items-center gap-1.5 text-xs text-destructive">
                  <AlertCircle className="h-3 w-3" />
                  {paramError}
                </span>
              ) : null}
              {runMutation.isSuccess && !paramError ? (
                <span className="flex items-center gap-1.5 text-xs text-long">
                  <CheckCircle2 className="h-3 w-3" />
                  Run complete - showing results below.
                </span>
              ) : null}
            </div>
          </CardContent>
        </Card>

        {/* Active run KPIs + chart */}
        {report && manifest ? (
          <ReportPanel report={report} manifest={manifest} />
        ) : detailQuery.isLoading && activeRunId ? (
          <Card>
            <CardContent className="p-8">
              <EmptyState
                icon={Activity}
                title="Loading run..."
                description={`Fetching report for ${activeRunId}.`}
              />
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="p-8">
              <EmptyState
                icon={FlaskConical}
                title="No run selected"
                description="Submit a run above or pick one from the history below."
              />
            </CardContent>
          </Card>
        )}

        {/* Run history */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <History className="h-4 w-4 text-primary" />
              Run history
            </CardTitle>
            <CardDescription>
              Last {runsQuery.data?.summary.count ?? 0} runs persisted under{" "}
              <code className="font-mono text-[11px]">
                {runsQuery.data?.summary.reports_root ?? "reports/backtests"}
              </code>
              .
            </CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            {!runsQuery.data?.runs?.length ? (
              <div className="px-6 pb-6">
                <EmptyState
                  icon={History}
                  title="No runs yet"
                  description="Submit a run above to see it appear here."
                />
              </div>
            ) : (
              <ScrollArea className="h-72">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 z-10 bg-card text-[11px] uppercase tracking-widest text-muted-foreground">
                    <tr className="border-b border-border/40">
                      <th className="px-6 py-2 text-left font-medium">Run id</th>
                      <th className="px-3 py-2 text-left font-medium">Strategy</th>
                      <th className="px-3 py-2 text-right font-medium">Bars</th>
                      <th className="px-3 py-2 text-right font-medium">Fills</th>
                      <th className="px-3 py-2 text-right font-medium">Return</th>
                      <th className="px-3 py-2 text-right font-medium">Sharpe</th>
                      <th className="px-3 py-2 text-right font-medium">Max DD</th>
                      <th className="px-6 py-2 text-right font-medium">Started</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runsQuery.data.runs.map((m) => (
                      <tr
                        key={m.run_id}
                        onClick={() => setActiveRunId(m.run_id)}
                        className={cn(
                          "cursor-pointer border-b border-border/30 transition-colors hover:bg-accent/40",
                          m.run_id === activeRunId
                            ? "bg-primary/5"
                            : undefined,
                        )}
                      >
                        <td className="px-6 py-2 font-mono text-[11px]">
                          {m.run_id.slice(0, 12)}
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">
                          {m.strategy_name}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {m.n_bars.toLocaleString()}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {m.n_fills.toLocaleString()}
                        </td>
                        <td
                          className={cn(
                            "px-3 py-2 text-right font-mono",
                            m.total_return_pct > 0
                              ? "text-long"
                              : m.total_return_pct < 0
                                ? "text-destructive"
                                : "text-muted-foreground",
                          )}
                        >
                          {m.total_return_pct > 0 ? "+" : ""}
                          {m.total_return_pct.toFixed(2)}%
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {m.sharpe == null ? "n/a" : m.sharpe.toFixed(2)}
                        </td>
                        <td className="px-3 py-2 text-right font-mono">
                          {m.max_drawdown_pct == null
                            ? "n/a"
                            : `${m.max_drawdown_pct.toFixed(2)}%`}
                        </td>
                        <td className="px-6 py-2 text-right font-mono text-[11px] text-muted-foreground">
                          {new Date(m.started_at * 1000).toLocaleString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollArea>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}

// --------------------------------------------------------------------------- //
// Helpers                                                                     //
// --------------------------------------------------------------------------- //

function FormField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      {children}
      {hint ? (
        <span className="text-[11px] text-muted-foreground">{hint}</span>
      ) : null}
    </label>
  );
}

function ReportPanel({
  report,
  manifest,
}: {
  report: BacktestReport;
  manifest: BacktestManifest;
}) {
  const equityData = useMemo(
    () =>
      report.equity_curve.map((p: BacktestEquityPoint) => ({
        ts: p.ts_event,
        equity: p.equity_usd,
        date: new Date(p.ts_event / 1_000_000).toLocaleString("en-US", {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        }),
      })),
    [report.equity_curve],
  );
  const positiveReturn = report.total_return_pct >= 0;
  const TrendIcon = positiveReturn ? TrendingUp : TrendingDown;
  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiCard label="Total return">
          <div
            className={cn(
              "flex items-baseline gap-2 text-2xl font-bold",
              positiveReturn ? "text-long" : "text-destructive",
            )}
          >
            <TrendIcon className="h-4 w-4" />
            {positiveReturn ? "+" : ""}
            {report.total_return_pct.toFixed(2)}%
          </div>
          <div className="text-xs text-muted-foreground">
            {formatUsd(report.starting_cash, { compact: true })}{" "}
            {"->"}{" "}
            {formatUsd(report.final_equity, { compact: true })}
          </div>
        </KpiCard>
        <KpiCard label="Sharpe (annualized)">
          <div className="text-2xl font-bold">
            {report.sharpe == null ? "n/a" : report.sharpe.toFixed(2)}
          </div>
          <div className="text-xs text-muted-foreground">
            {report.bars_per_year.toLocaleString()} bars/yr ({manifest.freq})
          </div>
        </KpiCard>
        <KpiCard label="Max drawdown">
          <div className="text-2xl font-bold text-warn">
            {report.max_drawdown_pct == null
              ? "n/a"
              : `-${report.max_drawdown_pct.toFixed(2)}%`}
          </div>
          <div className="text-xs text-muted-foreground">
            {report.longest_drawdown_bars ?? 0} bars in drawdown
          </div>
        </KpiCard>
        <KpiCard label="Trade activity">
          <div className="text-2xl font-bold">
            {report.n_fills.toLocaleString()}
          </div>
          <div className="text-xs text-muted-foreground">
            fills · {formatUsd(report.fees_paid_total, { compact: true })} fees
          </div>
        </KpiCard>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Activity className="h-4 w-4 text-primary" />
            Equity curve
          </CardTitle>
          <CardDescription>
            {manifest.strategy_name} · {manifest.symbols.join(", ")} ·{" "}
            {report.n_bars.toLocaleString()} bars
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-72 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={equityData}>
                <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="2 4" />
                <XAxis
                  dataKey="date"
                  fontSize={10}
                  tick={{ fill: "hsl(var(--muted-foreground))" }}
                  tickLine={false}
                  axisLine={{ stroke: "hsl(var(--border))" }}
                  minTickGap={32}
                />
                <YAxis
                  fontSize={10}
                  tick={{ fill: "hsl(var(--muted-foreground))" }}
                  tickLine={false}
                  axisLine={{ stroke: "hsl(var(--border))" }}
                  domain={["auto", "auto"]}
                  tickFormatter={(v) =>
                    `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`
                  }
                />
                <Tooltip
                  contentStyle={{
                    background: "hsl(var(--card))",
                    border: "1px solid hsl(var(--border))",
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  formatter={(v: number) =>
                    formatUsd(v, { compact: false })
                  }
                />
                <Line
                  type="monotone"
                  dataKey="equity"
                  stroke={
                    positiveReturn ? "hsl(var(--long))" : "hsl(var(--destructive))"
                  }
                  strokeWidth={1.5}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="normal-case tracking-normal">
            Per-symbol activity
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {report.per_symbol.length === 0 ? (
            <div className="p-6 text-sm text-muted-foreground">
              No fills produced.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-[11px] uppercase tracking-widest text-muted-foreground">
                <tr className="border-b border-border/40">
                  <th className="px-6 py-2 text-left font-medium">Symbol</th>
                  <th className="px-3 py-2 text-right font-medium">Fills</th>
                  <th className="px-3 py-2 text-right font-medium">Bought</th>
                  <th className="px-3 py-2 text-right font-medium">Sold</th>
                  <th className="px-3 py-2 text-right font-medium">
                    Notional traded
                  </th>
                  <th className="px-6 py-2 text-right font-medium">Fees</th>
                </tr>
              </thead>
              <tbody>
                {report.per_symbol.map((ps: BacktestPerSymbolStats) => (
                  <tr key={ps.symbol} className="border-b border-border/30">
                    <td className="px-6 py-2 font-mono text-xs">{ps.symbol}</td>
                    <td className="px-3 py-2 text-right font-mono">
                      {ps.fills.toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {ps.bought_qty.toFixed(4)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {ps.sold_qty.toFixed(4)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {formatUsd(ps.notional_traded, { compact: true })}
                    </td>
                    <td className="px-6 py-2 text-right font-mono">
                      {formatUsd(ps.fees_paid, { compact: true })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {report.trades.length > 0 ? (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="normal-case tracking-normal">
              Trades
            </CardTitle>
            <Badge variant="muted">{report.trades.length}</Badge>
          </CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-64">
              <table className="w-full text-sm">
                <thead className="sticky top-0 z-10 bg-card text-[11px] uppercase tracking-widest text-muted-foreground">
                  <tr className="border-b border-border/40">
                    <th className="px-6 py-2 text-left font-medium">When</th>
                    <th className="px-3 py-2 text-left font-medium">Symbol</th>
                    <th className="px-3 py-2 text-left font-medium">Side</th>
                    <th className="px-3 py-2 text-right font-medium">Qty</th>
                    <th className="px-3 py-2 text-right font-medium">Price</th>
                    <th className="px-6 py-2 text-right font-medium">Fee</th>
                  </tr>
                </thead>
                <tbody>
                  {report.trades.slice(0, 200).map((t) => (
                    <tr key={t.fill_id} className="border-b border-border/30">
                      <td className="px-6 py-1.5 font-mono text-[11px] text-muted-foreground">
                        {new Date(t.ts_event / 1_000_000).toLocaleString()}
                      </td>
                      <td className="px-3 py-1.5 font-mono text-xs">
                        {t.symbol}
                      </td>
                      <td
                        className={cn(
                          "px-3 py-1.5 font-mono text-xs uppercase",
                          t.side === "buy" ? "text-long" : "text-destructive",
                        )}
                      >
                        {t.side}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono">
                        {t.quantity.toFixed(4)}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono">
                        {formatUsd(t.price, { compact: false })}
                      </td>
                      <td className="px-6 py-1.5 text-right font-mono">
                        {formatUsd(t.fee, { compact: true })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollArea>
          </CardContent>
        </Card>
      ) : null}
    </>
  );
}

function KpiCard({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-1 p-4">
        <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
          {label}
        </span>
        {children}
      </CardContent>
    </Card>
  );
}
