"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart3, Database, Loader2, Newspaper, Rocket, Search } from "lucide-react";
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

import { SourceHealthControlCenter } from "@/components/data/source-health-control-center";
import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatUsd } from "@/lib/utils";

const FREQS = ["1m", "1h", "1d"] as const;

export default function MarketsPage() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [freq, setFreq] = useState<(typeof FREQS)[number]>("1m");
  const [alpacaSymbols, setAlpacaSymbols] = useState("AAPL,NVDA");

  const { data: universe } = useQuery({
    queryKey: ["universe"],
    queryFn: () => api.universe(token),
    enabled: !!token,
  });
  const { data: coverage } = useQuery({
    queryKey: ["data-coverage", freq],
    queryFn: () =>
      api.dataCoverage(token, {
        freq,
        lookback_ns:
          freq === "1d"
            ? 90 * 24 * 60 * 60 * 1_000_000_000
            : freq === "1h"
              ? 14 * 24 * 60 * 60 * 1_000_000_000
              : 24 * 60 * 60 * 1_000_000_000,
        stale_after_ns:
          freq === "1d"
            ? 3 * 24 * 60 * 60 * 1_000_000_000
            : freq === "1h"
              ? 6 * 60 * 60 * 1_000_000_000
              : 60 * 60 * 1_000_000_000,
      }),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const { data: sources } = useQuery({
    queryKey: ["data", "sources", "markets"],
    queryFn: () => api.dataSources(token),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const { data: services } = useQuery({
    queryKey: ["services", "markets"],
    queryFn: () => api.services(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const { data: openbbHealth } = useQuery({
    queryKey: ["openbb", "health", "markets"],
    queryFn: () => api.openbbHealth(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const { data: providerData } = useQuery({
    queryKey: ["provider-data", "markets"],
    queryFn: () => api.providerData(token, { limit: 12 }),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const seedMutation = useMutation({
    mutationFn: () => api.seedUniverseFromPositions(token),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["universe"] });
      queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
    },
  });
  const autopilotMutation = useMutation({
    mutationFn: async () => {
      const seeded = await api.seedUniverseFromPositions(token);
      const started = await api.startFeature(token, "market_data");
      return { seeded, started };
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["universe"] });
      queryClient.invalidateQueries({ queryKey: ["services"] });
      queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
      window.setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["data-coverage"] });
      }, 2500);
    },
  });
  const alpacaDemoMutation = useMutation({
    mutationFn: () =>
      api.alpacaDataDemo(token, {
        symbols: alpacaSymbols,
        news_limit: 5,
        bar_limit: 12,
      }),
  });

  const symbols = useMemo(
    () =>
      (universe ?? [])
        .filter((u) =>
          filter
            ? u.symbol.toLowerCase().includes(filter.toLowerCase())
            : true,
        )
        .sort((a, b) => a.symbol.localeCompare(b.symbol)),
    [universe, filter],
  );
  const fallbackSymbol = symbols[0]?.symbol ?? null;
  const selectedStillVisible = useMemo(
    () => !!selected && symbols.some((row) => row.symbol === selected),
    [selected, symbols],
  );

  // Default-select the first symbol when the universe loads.
  useEffect(() => {
    if (!fallbackSymbol) {
      if (selected !== null) setSelected(null);
      return;
    }
    if (!selectedStillVisible) {
      setSelected(fallbackSymbol);
    }
  }, [fallbackSymbol, selected, selectedStillVisible]);

  const range = useMemo(() => {
    const end = Date.now() * 1_000_000;
    const span =
      freq === "1d"
        ? 30 * 24 * 60 * 60 * 1_000_000_000
        : freq === "1h"
          ? 7 * 24 * 60 * 60 * 1_000_000_000
          : 6 * 60 * 60 * 1_000_000_000;
    return { start: end - span, end };
  }, [freq]);

  const { data: bars } = useQuery({
    queryKey: ["bars", selected, freq, range.start, range.end],
    queryFn: () =>
      api.bars(token, selected!, {
        start: range.start,
        end: range.end,
        freq,
      }),
    enabled: !!token && !!selected,
  });

  const chart = (bars ?? []).map((b) => ({
    t: Number(b.ts_event) / 1_000_000,
    close: Number(b.close),
    high: Number(b.high),
    low: Number(b.low),
  }));

  const last = chart[chart.length - 1];
  const first = chart[0];
  const pct =
    last && first && Number(first.close) !== 0
      ? ((last.close - first.close) / first.close) * 100
      : null;
  const alpacaDemo = alpacaDemoMutation.data ?? null;
  const alpacaDemoError =
    alpacaDemoMutation.error instanceof Error ? alpacaDemoMutation.error.message : null;
  const canRunAlpacaDemo =
    !!token && alpacaSymbols.trim().length > 0 && !alpacaDemoMutation.isPending;

  return (
    <AppShell>
      <PageHeader
        title="Markets"
        description="Universe browser, bar chart, and data coverage. Bars come from /data/bars with PIT-clean Timescale reads."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="muted">{symbols.length} symbols</Badge>
            <Button
              type="button"
              size="sm"
              className="gap-1.5"
              disabled={!token || autopilotMutation.isPending}
              onClick={() => autopilotMutation.mutate()}
            >
              {autopilotMutation.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Rocket className="h-3 w-3" />
              )}
              {autopilotMutation.isPending ? "Preparing…" : "Data autopilot"}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-1.5"
              disabled={!token || seedMutation.isPending}
              onClick={() => seedMutation.mutate()}
            >
              {seedMutation.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Database className="h-3 w-3" />
              )}
              {seedMutation.isPending ? "Seeding…" : "Seed from positions"}
            </Button>
          </div>
        }
      />

      {seedMutation.isError ? (
        <div className="mb-3 border border-short/40 bg-short/10 p-3 text-xs text-short">
          Failed to seed universe:{" "}
          {String((seedMutation.error as Error)?.message ?? "unknown error")}
        </div>
      ) : null}
      {autopilotMutation.isError ? (
        <div className="mb-3 border border-short/40 bg-short/10 p-3 text-xs text-short">
          Data autopilot failed:{" "}
          {String((autopilotMutation.error as Error)?.message ?? "unknown error")}
        </div>
      ) : null}
      {autopilotMutation.isSuccess ? (
        <div className="mb-3 border border-long/40 bg-long/10 p-3 text-xs text-long">
          Data autopilot requested market data after seeding{" "}
          {autopilotMutation.data.seeded.seeded} symbols.
        </div>
      ) : null}

      <SourceHealthControlCenter
        sources={sources ?? null}
        coverage={coverage ?? null}
        openbb={openbbHealth ?? null}
        providerData={providerData ?? null}
        services={services ?? null}
      />

      <Card className="mb-4 overflow-hidden border-primary/20">
        <CardHeader className="flex flex-row items-center justify-between gap-3 pb-3">
          <div>
            <CardTitle className="gap-2">
              <Newspaper className="h-4 w-4 text-primary" />
              Alpaca data demo
            </CardTitle>
            <div className="mt-1 text-[10px] normal-case text-muted-foreground">
              Read-only sample from Alpaca news and IEX 1-minute bars.
            </div>
          </div>
          <Badge variant="outline">No order path</Badge>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-2 md:grid-cols-[1fr_auto]">
            <Input
              value={alpacaSymbols}
              onChange={(event) => setAlpacaSymbols(event.target.value.toUpperCase())}
              onKeyDown={(event) => {
                if (event.key === "Enter" && canRunAlpacaDemo) {
                  alpacaDemoMutation.mutate();
                }
              }}
              placeholder="AAPL,NVDA"
              className="font-mono uppercase"
            />
            <Button
              type="button"
              disabled={!canRunAlpacaDemo}
              onClick={() => alpacaDemoMutation.mutate()}
            >
              {alpacaDemoMutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Database className="h-3.5 w-3.5" />
              )}
              Run demo
            </Button>
          </div>
          {alpacaDemoError ? (
            <div className="border border-short/40 bg-short/10 p-3 text-xs text-short">
              Alpaca demo failed: {alpacaDemoError}
            </div>
          ) : null}
          {alpacaDemo ? (
            <div className="grid gap-3 lg:grid-cols-[16rem_1fr_18rem]">
              <div className="grid grid-cols-3 gap-2 lg:grid-cols-1">
                <DemoMetric label="News" value={String(alpacaDemo.summary.news_count)} />
                <DemoMetric label="Bars" value={String(alpacaDemo.summary.bar_count)} />
                <DemoMetric label="Feed" value={alpacaDemo.feed.toUpperCase()} />
              </div>
              <div className="space-y-2">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  Recent headlines
                </div>
                {alpacaDemo.news.length ? (
                  <div className="grid gap-2">
                    {alpacaDemo.news.slice(0, 5).map((row, index) => (
                      <div
                        key={`${getText(row, "id") ?? index}`}
                        className="border border-border/50 bg-background/40 p-2"
                      >
                        <div className="text-sm font-medium text-foreground">
                          {getText(row, "headline", "title") ?? "Untitled"}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
                          <span>{getText(row, "source") ?? "Alpaca news"}</span>
                          <span>{getText(row, "created_at", "updated_at") ?? "recent"}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="border border-dashed border-border p-3 text-xs text-muted-foreground">
                    No news rows returned for this symbol set.
                  </div>
                )}
              </div>
              <div className="space-y-2">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  Bar sample
                </div>
                <div className="grid gap-2">
                  {alpacaDemo.symbols.map((symbol) => {
                    const rows = alpacaDemo.bars[symbol] ?? [];
                    const lastBar = rows[rows.length - 1];
                    return (
                      <div
                        key={symbol}
                        className="flex items-center justify-between border border-border/50 bg-background/40 p-2"
                      >
                        <span className="font-mono text-sm text-foreground">{symbol}</span>
                        <span className="text-right text-xs text-muted-foreground">
                          {rows.length} bars
                          {lastBar ? (
                            <span className="ml-2 font-mono text-primary">
                              {formatBarClose(lastBar)}
                            </span>
                          ) : null}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <div className="mb-4 grid gap-2 md:grid-cols-4">
        <CoverageTile label="Coverage" value={`${coverage?.summary.coverage_pct ?? 0}%`} tone="primary" />
        <CoverageTile label="Fresh" value={String(coverage?.summary.ok ?? 0)} tone="long" />
        <CoverageTile label="Stale" value={String(coverage?.summary.stale ?? 0)} tone="warn" />
        <CoverageTile label="Empty" value={String(coverage?.summary.empty ?? 0)} tone="short" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[20rem_1fr]">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle>Universe</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter…"
                className="pl-7"
              />
            </div>
            {symbols.length === 0 ? (
              <EmptyState
                icon={BarChart3}
                title="No symbols"
                description="The universe table is empty.  Seed via the ingestor or admin script."
              />
            ) : (
              <ScrollArea className="h-[28rem]">
                <ul className="space-y-0.5">
                  {symbols.map((u) => (
                    // Coverage is advisory; the chart remains the source of truth
                    // for the selected range.
                    <li key={u.symbol}>
                      <button
                        onClick={() => setSelected(u.symbol)}
                        className={cn(
                          "flex w-full items-center justify-between rounded-md border border-transparent px-3 py-1.5 text-left text-sm transition-colors hover:bg-accent",
                          selected === u.symbol &&
                            "border-primary/40 bg-primary/10",
                        )}
                      >
                        <span className="font-mono">{u.symbol}</span>
                        <div className="flex items-center gap-1">
                          <CoverageDot status={coverage?.rows.find((row) => row.symbol === u.symbol)?.status} />
                          <Badge variant="muted">{u.asset_class}</Badge>
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
            <div>
              <CardTitle className="normal-case tracking-normal">
                <span className="font-mono text-base">{selected ?? "—"}</span>
              </CardTitle>
              <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
                {last ? (
                  <span className="num text-foreground">
                    {formatUsd(last.close)}
                  </span>
                ) : null}
                {pct !== null ? (
                  <span
                    className={cn(
                      "num font-medium",
                      pct >= 0 ? "text-long" : "text-short",
                    )}
                  >
                    {pct >= 0 ? "+" : ""}
                    {pct.toFixed(2)}%
                  </span>
                ) : null}
              </div>
            </div>
            <div className="flex gap-1">
              {FREQS.map((f) => (
                <button
                  key={f}
                  onClick={() => setFreq(f)}
                  className={cn(
                    "rounded-md border border-border/60 bg-background/40 px-2 py-1 text-[11px] uppercase tracking-wider transition-colors",
                    freq === f
                      ? "border-primary/60 bg-primary/15 text-primary"
                      : "text-muted-foreground hover:bg-accent",
                  )}
                >
                  {f}
                </button>
              ))}
            </div>
          </CardHeader>
          <CardContent>
            {chart.length === 0 ? (
              <EmptyState
                icon={BarChart3}
                title={selected ? "No bars yet" : "Pick a symbol"}
                description={
                  selected
                    ? "Either no bars in window or DB unavailable."
                    : "Choose from the list on the left."
                }
              />
            ) : (
              <div className="h-[28rem]">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chart}>
                    <CartesianGrid
                      stroke="hsl(var(--border))"
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="t"
                      tickFormatter={(t) =>
                        new Date(t).toISOString().slice(11, 16)
                      }
                      stroke="hsl(var(--muted-foreground))"
                      fontSize={11}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      domain={["auto", "auto"]}
                      stroke="hsl(var(--muted-foreground))"
                      fontSize={11}
                      axisLine={false}
                      tickLine={false}
                      tickFormatter={(v: number) =>
                        v.toLocaleString("en-US", {
                          maximumFractionDigits: 2,
                        })
                      }
                    />
                    <Tooltip
                      contentStyle={{
                        background: "hsl(var(--card))",
                        border: "1px solid hsl(var(--border))",
                        borderRadius: 8,
                        fontSize: 11,
                      }}
                      labelFormatter={(t: number) =>
                        new Date(t).toISOString().replace("T", " ").slice(0, 19) +
                        "Z"
                      }
                      formatter={(value: number) => formatUsd(value)}
                    />
                    <Line
                      type="monotone"
                      dataKey="close"
                      stroke="hsl(var(--primary))"
                      strokeWidth={1.5}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}

function CoverageTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "primary" | "long" | "warn" | "short";
}) {
  return (
    <Card>
      <CardContent className="flex items-center justify-between p-3">
        <span className="text-xs uppercase tracking-widest text-muted-foreground">{label}</span>
        <span
          className={cn(
            "font-mono text-lg font-semibold",
            tone === "primary" && "text-primary",
            tone === "long" && "text-long",
            tone === "warn" && "text-warn",
            tone === "short" && "text-short",
          )}
        >
          {value}
        </span>
      </CardContent>
    </Card>
  );
}

function DemoMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-border/60 bg-background/45 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="font-mono text-lg font-semibold text-primary">{value}</div>
    </div>
  );
}

function getText(row: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return null;
}

function formatBarClose(row: Record<string, unknown>) {
  const value = row.c ?? row.close ?? row.Close;
  const numberValue =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value)
        : NaN;
  return Number.isFinite(numberValue) ? formatUsd(numberValue) : "—";
}

function CoverageDot({ status }: { status?: "ok" | "stale" | "empty" | "error" }) {
  return (
    <span
      title={status ?? "unknown"}
      className={cn(
        "h-2 w-2 rounded-full",
        status === "ok" && "bg-long",
        status === "stale" && "bg-warn",
        (status === "empty" || status === "error") && "bg-short",
        !status && "bg-muted-foreground/40",
      )}
    />
  );
}
