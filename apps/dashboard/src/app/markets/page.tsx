"use client";

import { useQuery } from "@tanstack/react-query";
import { BarChart3, Search } from "lucide-react";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatUsd } from "@/lib/utils";

const FREQS = ["1m", "1h", "1d"] as const;

export default function MarketsPage() {
  const token = useAuth((s) => s.token);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [freq, setFreq] = useState<(typeof FREQS)[number]>("1m");

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

  return (
    <AppShell>
      <PageHeader
        title="Markets"
        description="Universe browser, bar chart, and data coverage. Bars come from /data/bars with PIT-clean Timescale reads."
        action={<Badge variant="muted">{symbols.length} symbols</Badge>}
      />

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
