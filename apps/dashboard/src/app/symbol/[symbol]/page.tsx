"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  Brain,
  Calendar,
  ChevronRight,
  ExternalLink,
  Newspaper,
  ShieldCheck,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { formatDistanceToNowStrict } from "date-fns";

import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/widgets/empty-state";
import { LEDDot } from "@/components/widgets/led-dot";
import { MockBadge } from "@/components/widgets/mock-badge";
import { PageHeader } from "@/components/widgets/page-header";
import { SignalCard, SignalStrip } from "@/components/widgets/signal-card";
import {
  type ChartRange,
  TradingChart,
  type TradingChartPoint,
} from "@/components/widgets/trading-chart";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { directionOf, formatSignedPct, formatSignedUsd } from "@/lib/design-tokens";
import {
  mockPriceWalk,
  mockVolumeWalk,
  withMockFlag,
  type MockFlag,
} from "@/lib/mock-data";
import type { Position, PredictionRow } from "@/lib/types";
import { cn, formatNumber, formatUsd, nsToDate } from "@/lib/utils";

/**
 * /symbol/[symbol] — stock detail page.
 *
 * Bloomberg/TradingView-style layout.  Sections:
 *
 *   [1]  Header  — symbol, name, last price, change, MOCK chip, action
 *   [2]  TradingChart  — area + volume, range chips (mock data)
 *   [3]  Quick stats  — 52w hi/lo, ADV, mkt cap, beta, P/E
 *   [4]  Your position  — only if you have one (real API)
 *   [5]  Active signals  — predictions filtered to this symbol (real WS)
 *   [6]  Recent news     — mock until the news API is wired per-symbol
 *   [7]  Strategy exposure — which strategies touch this symbol
 *
 * Mock sections are clearly marked; live sections pull from real
 * API + WebSocket.  This is the model the rest of the app should
 * converge on: live data with mock fallbacks that scream "MOCK".
 */

interface SymbolMock {
  name: string;
  cap: string;
  mktCap: number;
  peRatio: number;
  beta: number;
  high52w: number;
  low52w: number;
  adv: number;
  dividendYieldPct: number;
}

interface MockSignal {
  id: string;
  title: string;
  source: "model" | "system" | "human";
  direction: number;
  confidence: number;
  context: string;
  ts: number;
  flag: MockFlag;
}

interface MockNews {
  id: string;
  headline: string;
  source: string;
  ts: number;
  flag: MockFlag;
}

function buildSymbolMock(symbol: string): {
  meta: SymbolMock;
  signals: MockSignal[];
  news: MockNews[];
} {
  // Deterministic hash from the symbol so re-renders are stable.
  let hash = 0;
  for (let i = 0; i < symbol.length; i++) {
    hash = (hash * 31 + symbol.charCodeAt(i)) >>> 0;
  }
  const meta: SymbolMock = {
    name: nameLookup(symbol),
    cap: capLookup(symbol),
    mktCap: 50_000_000_000 + (hash % 1500) * 1_000_000_000,
    peRatio: 18 + (hash % 22),
    beta: 0.6 + ((hash % 100) / 100) * 1.4,
    high52w: 100 + (hash % 400),
    low52w: 30 + (hash % 60),
    adv: 2_000_000 + (hash % 30_000_000),
    dividendYieldPct: (hash % 350) / 100,
  };
  const now = Date.now() * 1_000_000;
  const signals: MockSignal[] = [
    {
      id: `${symbol}-s1`,
      title: "Momentum model turned bullish on 4H breakout",
      source: "model",
      direction: 0.62,
      confidence: 0.71,
      context: "gbm.v1 + news alpha agreement",
      ts: now - 8 * 60 * 1_000_000_000,
      flag: { source: "Inline fixture", ticket: "FIN-MOCK" },
    },
    {
      id: `${symbol}-s2`,
      title: "Risk parity suggests reducing weight",
      source: "system",
      direction: -0.18,
      confidence: 0.42,
      context: "vol-regime: high · corr: defensive",
      ts: now - 35 * 60 * 1_000_000_000,
      flag: { source: "Inline fixture", ticket: "FIN-MOCK" },
    },
    {
      id: `${symbol}-s3`,
      title: "Earnings window in 6 sessions",
      source: "model",
      direction: 0.12,
      confidence: 0.38,
      context: "earnings_vol_risk: 1.6x",
      ts: now - 4 * 3600 * 1_000_000_000,
      flag: { source: "Inline fixture", ticket: "FIN-MOCK" },
    },
  ];
  const news: MockNews[] = [
    {
      id: `${symbol}-n1`,
      headline: `${symbol} reports record quarterly revenue, raises guidance`,
      source: "Reuters",
      ts: now - 22 * 60 * 1_000_000_000,
      flag: { source: "Inline fixture", ticket: "FIN-MOCK" },
    },
    {
      id: `${symbol}-n2`,
      headline: `Analysts upgrade ${symbol} to overweight on margin expansion`,
      source: "Bloomberg",
      ts: now - 6 * 3600 * 1_000_000_000,
      flag: { source: "Inline fixture", ticket: "FIN-MOCK" },
    },
    {
      id: `${symbol}-n3`,
      headline: `${symbol} announces $5B buyback authorization`,
      source: "WSJ",
      ts: now - 26 * 3600 * 1_000_000_000,
      flag: { source: "Inline fixture", ticket: "FIN-MOCK" },
    },
  ];
  return { meta, signals, news };
}

function nameLookup(symbol: string): string {
  const map: Record<string, string> = {
    AAPL: "Apple Inc.",
    NVDA: "NVIDIA Corp.",
    MSFT: "Microsoft Corp.",
    GOOGL: "Alphabet Inc.",
    META: "Meta Platforms",
    AMZN: "Amazon.com Inc.",
    TSLA: "Tesla Inc.",
    AMD: "Advanced Micro Devices",
    AVGO: "Broadcom Inc.",
    JPM: "JPMorgan Chase",
    V: "Visa Inc.",
    XOM: "Exxon Mobil",
    COIN: "Coinbase Global",
    PLTR: "Palantir Technologies",
    SMCI: "Super Micro Computer",
    ARM: "Arm Holdings",
    MSTR: "MicroStrategy",
    SPY: "S&P 500 ETF",
    QQQ: "Invesco QQQ Trust",
    IBIT: "iShares Bitcoin Trust",
  };
  return map[symbol.toUpperCase()] ?? `${symbol} Corp.`;
}

function capLookup(symbol: string): string {
  const map: Record<string, string> = {
    AAPL: "MEGA", NVDA: "MEGA", MSFT: "MEGA", GOOGL: "MEGA",
    META: "MEGA", AMZN: "MEGA", TSLA: "MEGA", AVGO: "MEGA",
    V: "MEGA", AMD: "LARGE", JPM: "LARGE", XOM: "LARGE",
    ARM: "LARGE", COIN: "MID", PLTR: "MID", SMCI: "MID",
    MSTR: "MID",
    SPY: "ETF", QQQ: "ETF", IBIT: "ETF",
  };
  return map[symbol.toUpperCase()] ?? "MID";
}

function buildChartForSymbol(symbol: string, range: ChartRange): {
  data: TradingChartPoint[];
  first: number;
  last: number;
} {
  // Different bar counts per range; deterministic per symbol+range.
  const points = range === "1D" ? 78 : range === "1W" ? 168 : range === "1M" ? 30 : range === "3M" ? 90 : 240;
  let hash = 0;
  for (let i = 0; i < symbol.length; i++) hash = (hash * 31 + symbol.charCodeAt(i)) >>> 0;
  const seed = hash ^ (range.charCodeAt(0) * 7919);
  const start = 100 + (hash % 200);
  const walk = mockPriceWalk({ seed, count: points, start, volatility: 0.018, drift: 0.0008 });
  const vol = mockVolumeWalk({ seed, count: points, baseVolume: 1_200_000, volatility: 0.45 });
  // Map x to a real timestamp (last 30 days) so the axis makes sense.
  const now = Date.now();
  const span = range === "1D" ? 6.5 * 3600_000
    : range === "1W" ? 7 * 24 * 3600_000
    : range === "1M" ? 30 * 24 * 3600_000
    : range === "3M" ? 90 * 24 * 3600_000
    : 365 * 24 * 3600_000;
  const step = span / points;
  const data: TradingChartPoint[] = walk.map((p, i) => {
    const v = vol[i]?.y ?? 1_000_000;
    return {
      x: now - span + i * step,
      close: p.y,
      high: p.y * 1.005,
      low: p.y * 0.995,
      volume: v,
    };
  });
  return { data, first: data[0].close, last: data[data.length - 1].close };
}

export default function SymbolPage({ params }: { params: { symbol: string } }) {
  const symbol = decodeURIComponent(params.symbol).toUpperCase();
  const token = useAuth((s) => s.token);

  // Live data
  const positionsQ = useQuery({
    queryKey: ["positions", "all"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 15_000,
  });
  const predictionsQ = useQuery({
    queryKey: ["models", "gbm_predictor", "predictions", "symbol"],
    queryFn: () => api.modelPredictions(token, "gbm_predictor", { limit: 60 }),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  // Mock data
  const { meta, signals, news } = useMemo(() => buildSymbolMock(symbol), [symbol]);

  // Per-symbol position aggregation
  const myPosition: Position | null = useMemo(() => {
    const rows = positionsQ.data ?? [];
    return rows.find(
      (p) => p.symbol.toUpperCase() === symbol && Number(p.quantity) !== 0,
    ) ?? null;
  }, [positionsQ.data, symbol]);

  // Per-symbol predictions (live)
  const livePredictions: PredictionRow[] = useMemo(() => {
    const rows = predictionsQ.data?.predictions ?? [];
    return rows.filter((r) => r.symbol.toUpperCase() === symbol);
  }, [predictionsQ.data, symbol]);

  // Chart state
  const [range, setRange] = useState<ChartRange>("1M");
  const chart = useMemo(() => buildChartForSymbol(symbol, range), [symbol, range]);
  const change = chart.last - chart.first;
  const changePct = chart.first !== 0 ? (change / chart.first) * 100 : 0;
  const direction = directionOf(change);

  return (
    <AppShell>
      <PageHeader
        title={
          <span className="flex items-center gap-3">
            <Link
              href="/watchlist"
              className="inline-flex h-6 w-6 items-center justify-center border border-hairline text-muted-foreground transition-colors hover:border-cobalt-soft hover:text-foreground"
              aria-label="Back to watchlist"
            >
              <ArrowLeft className="h-3 w-3" />
            </Link>
            <span className="font-mono text-2xl font-semibold tracking-tight text-cobalt">
              {symbol}
            </span>
            <span className="text-base font-normal text-foreground/90">{meta.name}</span>
            <Badge variant="outline" className="text-[10px]">
              {meta.cap}
            </Badge>
            <MockBadge source="Mock metadata" />
          </span>
        }
        description="Per-symbol overview: price action, your exposure, live signals, related news. Mock sections are clearly flagged until each API ships."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="long">
              <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-long" />
              API LIVE
            </Badge>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              asChild
            >
              <Link href={`/markets?symbol=${encodeURIComponent(symbol)}`}>
                <ExternalLink className="h-3 w-3" />
                Open in Markets
              </Link>
            </Button>
          </div>
        }
      />

      {/* Hero — price block + LEDDot + quick stats */}
      <div className="glass-hero relative mb-3 overflow-hidden p-4">
        <span className="pointer-events-none absolute inset-x-0 top-0 h-px bg-cobalt/60" />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_auto]">
          <div className="flex flex-wrap items-end gap-6">
            <div>
              <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
                Last
              </div>
              <div className="num flex items-baseline gap-2 font-mono text-4xl font-bold tabular-nums text-foreground">
                {formatUsd(chart.last)}
                <LEDDot tone={direction === "up" ? "long" : direction === "down" ? "short" : "muted"} pulse size="lg" />
              </div>
              <div
                className={cn(
                  "num mt-0.5 flex items-center gap-1.5 font-mono text-sm font-semibold tabular-nums",
                  change > 0 ? "text-long" : change < 0 ? "text-short" : "text-muted-foreground",
                )}
              >
                {change > 0 ? (
                  <ArrowUpRight className="h-4 w-4" />
                ) : change < 0 ? (
                  <ArrowDownRight className="h-4 w-4" />
                ) : null}
                {formatSignedUsd(change)} ({formatSignedPct(changePct)})
              </div>
            </div>
            <div>
              <div className="font-mono text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
                Range {range}
              </div>
              <div className="num font-mono text-base font-semibold tabular-nums text-foreground">
                {formatUsd(Math.min(...chart.data.map((d) => d.close)))} – {formatUsd(Math.max(...chart.data.map((d) => d.close)))}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-0 self-start border border-hairline bg-background/30 md:grid-cols-4 md:border-l md:border-r-0 md:border-t-0 md:border-b-0">
            <QuickStat label="52W HIGH" value={formatUsd(meta.high52w)} tone="long" />
            <QuickStat label="52W LOW" value={formatUsd(meta.low52w)} tone="short" />
            <QuickStat
              label="MKT CAP"
              value={formatUsd(meta.mktCap, { compact: true })}
              tone="info"
            />
            <QuickStat label="P/E" value={formatNumber(meta.peRatio, 1)} tone="info" />
            <QuickStat label="BETA" value={formatNumber(meta.beta, 2)} tone="info" />
            <QuickStat
              label="ADV"
              value={Intl.NumberFormat("en-US", { notation: "compact" }).format(meta.adv)}
              tone="info"
            />
            <QuickStat
              label="YIELD"
              value={`${meta.dividendYieldPct.toFixed(2)}%`}
              tone={meta.dividendYieldPct > 0 ? "long" : "muted"}
            />
            <QuickStat
              label="SIGNALS"
              value={`${livePredictions.length} live · ${signals.length} mock`}
              tone="info"
            />
          </div>
        </div>
      </div>

      {/* Chart */}
      <div className="mb-3">
        <TradingChart
          data={chart.data}
          direction={direction}
          range={range}
          onRangeChange={setRange}
          symbol={symbol}
          isMock
        />
      </div>

      {/* Two-column grid: position + signals + news */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        {/* Your position */}
        <Card className="lg:col-span-1">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-1">
              <ShieldCheck className="h-3.5 w-3.5" />
              Your position
              {myPosition ? null : (
                <Badge variant="muted" className="ml-1">None</Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!myPosition ? (
              <EmptyState
                icon={ShieldCheck}
                title="Flat on this symbol"
                description="Your exposure will appear here when an OMS fill lands."
                className="border-0 bg-transparent p-3"
              />
            ) : (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                    Qty
                  </span>
                  <span className="num font-mono text-sm font-semibold tabular-nums">
                    {formatNumber(myPosition.quantity, 6)}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                    Avg cost
                  </span>
                  <span className="num font-mono text-sm tabular-nums">
                    {formatUsd(myPosition.avg_cost)}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                    Unrealized
                  </span>
                  <span
                    className={cn(
                      "num font-mono text-sm font-semibold tabular-nums",
                      Number(myPosition.unrealized_pnl) > 0
                        ? "text-long"
                        : Number(myPosition.unrealized_pnl) < 0
                          ? "text-short"
                          : "text-muted-foreground",
                    )}
                  >
                    {formatUsd(myPosition.unrealized_pnl, { signed: true })}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                    Realized
                  </span>
                  <span
                    className={cn(
                      "num font-mono text-sm tabular-nums",
                      Number(myPosition.realized_pnl) > 0
                        ? "text-long"
                        : Number(myPosition.realized_pnl) < 0
                          ? "text-short"
                          : "text-muted-foreground",
                    )}
                  >
                    {formatUsd(myPosition.realized_pnl, { signed: true })}
                  </span>
                </div>
                <div className="mt-2 flex items-center justify-between border-t border-hairline pt-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                  <span>Strategy</span>
                  <Link
                    href={`/strategies/${encodeURIComponent(myPosition.strategy_id)}`}
                    className="inline-flex items-center gap-1 text-cobalt hover:underline"
                  >
                    {myPosition.strategy_id}
                    <ChevronRight className="h-3 w-3" />
                  </Link>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Signals */}
        <div className="space-y-2 lg:col-span-2">
          <div className="flex items-center justify-between">
            <h2 className="flex items-center gap-2 font-mono text-[11px] font-bold uppercase tracking-widest text-muted-foreground">
              <Brain className="h-3.5 w-3.5" />
              Signals
            </h2>
            <div className="flex items-center gap-2">
              <MockBadge source="Inline fixture" />
              <Link
                href="/predictions"
                className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-cobalt hover:underline"
              >
                All predictions
                <ChevronRight className="h-3 w-3" />
              </Link>
            </div>
          </div>

          {livePredictions.length > 0 ? (
            <div className="mb-2 space-y-1.5">
              {livePredictions.slice(0, 2).map((p) => (
                <SignalStrip
                  key={`${p.id}:${p.symbol}:${p.ts_event}`}
                  direction={p.direction}
                  confidence={p.confidence}
                  symbol={p.symbol}
                />
              ))}
            </div>
          ) : null}

          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {signals.map((s) => (
              <SignalCard
                key={s.id}
                kind="signal"
                title={s.title}
                symbol={symbol}
                direction={s.direction}
                confidence={s.confidence}
                source={s.source}
                context={s.context}
                ts={s.ts}
                isMock
                href={`/predictions?symbol=${encodeURIComponent(symbol)}`}
              />
            ))}
          </div>
        </div>
      </div>

      {/* News */}
      <div className="mt-3">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-mono text-[11px] font-bold uppercase tracking-widest text-muted-foreground">
            <Newspaper className="h-3.5 w-3.5" />
            Related news
          </h2>
          <div className="flex items-center gap-2">
            <MockBadge source="Inline fixture" />
            <Link
              href={`/news?q=${encodeURIComponent(symbol)}`}
              className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-cobalt hover:underline"
            >
              All news
              <ChevronRight className="h-3 w-3" />
            </Link>
          </div>
        </div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          {news.map((n) => (
            <article
              key={n.id}
              className="group flex flex-col gap-1 border border-hairline bg-card/60 p-3 transition-colors hover:border-cobalt-soft hover:bg-card/80"
            >
              <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                <span>{n.source}</span>
                <span>·</span>
                <span>{ago(n.ts)}</span>
              </div>
              <h3 className="text-sm font-medium leading-snug text-foreground group-hover:text-cobalt">
                {n.headline}
              </h3>
            </article>
          ))}
        </div>
      </div>

      {/* Footer attribution */}
      <p className="mt-4 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
        Header price, chart, signals, and news are mock fixtures; replace
        each with its respective API + WS subscription. Position block
        reflects live data from /positions.
      </p>
    </AppShell>
  );
}

function QuickStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "long" | "short" | "info" | "muted";
}) {
  const cls =
    tone === "long" ? "text-long"
    : tone === "short" ? "text-short"
    : tone === "info" ? "text-cobalt"
    : "text-muted-foreground";
  return (
    <div className="flex flex-col gap-0.5 border-r border-hairline px-3 py-2 last:border-r-0">
      <span className="font-mono text-[9px] font-bold uppercase tracking-widest text-muted-foreground/80">
        {label}
      </span>
      <span className={cn("num font-mono text-sm font-semibold tabular-nums", cls)}>
        {value}
      </span>
    </div>
  );
}

function ago(ns: number): string {
  const d = nsToDate(ns);
  if (!d) return "—";
  try {
    return formatDistanceToNowStrict(d, { addSuffix: false });
  } catch {
    return "—";
  }
}
