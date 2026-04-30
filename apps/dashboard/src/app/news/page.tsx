"use client";

/**
 * /news — book-aware news terminal.
 *
 * Two lanes:
 *   1. IMPACT    stories that touch any position, sorted by |$ delta|.
 *   2. UNIVERSE  everything else, time-sorted.
 *
 * Each row shows: age · primary symbol (+ in-book badge) · headline +
 * other symbols · inline SVG sparkline from publish → now · signed $
 * impact on your book · source.  Auto-refetches every 10s.
 */

import { useQuery } from "@tanstack/react-query";
import { Briefcase, ExternalLink, Newspaper, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { NewsArticle, NewsSymbolImpact } from "@/lib/types";
import { cn, formatUsd, pnlClass } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function timeAgo(ns: number, now: number): string {
  const diffSec = Math.max(0, (now - ns / 1_000_000) / 1000);
  if (diffSec < 60) return `${Math.floor(diffSec)}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86_400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86_400)}d`;
}

function asNum(v: string | null | undefined): number {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function pickPrimary(symbols: NewsSymbolImpact[]): NewsSymbolImpact | null {
  if (!symbols.length) return null;
  const inBookWithImpact = symbols.filter(
    (s) => s.in_book && s.dollar_impact !== null,
  );
  if (inBookWithImpact.length) {
    return inBookWithImpact.reduce((best, s) =>
      Math.abs(asNum(s.dollar_impact)) > Math.abs(asNum(best.dollar_impact))
        ? s
        : best,
    );
  }
  const withPct = symbols.find((s) => s.pct_change !== null);
  return withPct ?? symbols[0];
}

// ---------------------------------------------------------------------------
// Inline SVG sparkline — dense, no recharts, no animation
// ---------------------------------------------------------------------------

function NewsSparkline({
  data,
  positive,
  width = 96,
  height = 22,
}: {
  data: number[];
  positive: boolean;
  width?: number;
  height?: number;
}) {
  if (data.length < 2) {
    return (
      <span className="inline-block text-[10px] text-muted-foreground/40">
        ···
      </span>
    );
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const stepX = width / (data.length - 1);
  const pts = data
    .map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const stroke = positive ? "hsl(var(--long))" : "hsl(var(--short))";
  const fill = positive
    ? "rgba(34, 197, 94, 0.10)"
    : "rgba(239, 68, 68, 0.10)";
  const areaPts = `0,${height} ${pts} ${width},${height}`;
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="inline-block align-middle"
    >
      <polygon points={areaPts} fill={fill} stroke="none" />
      <polyline
        points={pts}
        fill="none"
        stroke={stroke}
        strokeWidth={1}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

function ArticleRow({
  article,
  now,
  compact = false,
}: {
  article: NewsArticle;
  now: number;
  compact?: boolean;
}) {
  const primary = pickPrimary(article.symbols);
  const age = timeAgo(article.ts_event_ns, now);
  const totalImpact = article.total_dollar_impact
    ? Number(article.total_dollar_impact)
    : null;
  const pct = primary?.pct_change ?? null;
  const positive = (totalImpact ?? pct ?? 0) >= 0;
  const others = primary
    ? article.symbols.filter((s) => s.symbol !== primary.symbol)
    : article.symbols;

  return (
    <tr className="border-b border-border/30 align-top last:border-b-0 hover:bg-accent/30">
      <td className="w-12 px-2 py-2 text-right font-mono text-[11px] text-muted-foreground">
        {age}
      </td>
      <td className="w-24 px-2 py-2">
        {primary ? (
          <div className="flex flex-col items-start gap-0.5">
            <span
              className={cn(
                "inline-block px-1.5 py-[1px] font-mono text-[11px] leading-tight",
                primary.in_book
                  ? "bg-long/15 text-long"
                  : "bg-muted/30 text-muted-foreground",
              )}
            >
              {primary.symbol}
            </span>
            {pct !== null ? (
              <span
                className={cn(
                  "font-mono text-[10px] leading-none",
                  pnlClass(pct),
                )}
              >
                {pct > 0 ? "↑" : pct < 0 ? "↓" : "·"}
                {Math.abs(pct * 100).toFixed(1)}%
              </span>
            ) : null}
          </div>
        ) : (
          <span className="text-[11px] text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-2 py-2">
        <a
          href={article.url}
          target="_blank"
          rel="noreferrer"
          className="group block text-sm leading-snug hover:text-primary"
        >
          {article.headline}
          <ExternalLink className="ml-1 inline h-3 w-3 opacity-0 group-hover:opacity-60" />
        </a>
        {!compact && article.summary ? (
          <div className="mt-0.5 line-clamp-1 text-[11px] text-muted-foreground">
            {article.summary}
          </div>
        ) : null}
        {others.length > 0 ? (
          <div className="mt-1 flex flex-wrap gap-1">
            {others.slice(0, 6).map((s) => (
              <span
                key={s.symbol}
                className={cn(
                  "px-1 font-mono text-[9px] leading-tight",
                  s.in_book
                    ? "bg-long/10 text-long/80"
                    : "text-muted-foreground/60",
                )}
              >
                {s.symbol}
              </span>
            ))}
            {others.length > 6 ? (
              <span className="text-[9px] text-muted-foreground/60">
                +{others.length - 6}
              </span>
            ) : null}
          </div>
        ) : null}
      </td>
      <td className="w-28 px-2 py-2 text-right">
        {primary ? (
          <NewsSparkline data={primary.sparkline} positive={positive} />
        ) : null}
      </td>
      <td
        className={cn(
          "w-24 px-2 py-2 text-right font-mono text-[12px]",
          pnlClass(totalImpact),
        )}
      >
        {totalImpact !== null ? (
          formatUsd(totalImpact, { signed: true })
        ) : (
          <span className="text-muted-foreground/50">—</span>
        )}
      </td>
      <td className="w-24 px-2 py-2 text-right text-[10px] uppercase tracking-wider text-muted-foreground">
        {article.source || "—"}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function NewsPage() {
  const token = useAuth((s) => s.token);
  const [filter, setFilter] = useState("");
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(id);
  }, []);

  const query = useQuery({
    queryKey: ["news", 100],
    queryFn: () => api.news(token, { limit: 100 }),
    enabled: !!token,
    refetchInterval: 10_000,
    staleTime: 5_000,
  });

  const data = query.data;

  const filtered = useMemo(() => {
    if (!data) return { impact: [], universe: [] };
    const match = (a: NewsArticle) => {
      if (!filter) return true;
      const f = filter.toLowerCase();
      return (
        a.headline.toLowerCase().includes(f) ||
        a.source.toLowerCase().includes(f) ||
        a.symbols.some((s) => s.symbol.toLowerCase().includes(f))
      );
    };
    return {
      impact: data.impact.filter(match),
      universe: data.universe.filter(match),
    };
  }, [data, filter]);

  const bookTotal = data ? Number(data.book_total_impact) : 0;

  return (
    <AppShell>
      <PageHeader
        title="News"
        description="Book-aware news feed — stories that move your book surface first, with live price reaction and dollar impact per article."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="muted">
              {(data?.impact.length ?? 0) + (data?.universe.length ?? 0)} stories
            </Badge>
            <button
              onClick={() => query.refetch()}
              className="inline-flex items-center gap-1 rounded-md border border-border/60 bg-background/40 px-3 py-1.5 text-xs"
            >
              <RefreshCw
                className={cn("h-3 w-3", query.isFetching && "animate-spin")}
              />
              Refresh
            </button>
          </div>
        }
      />

      {/* Book impact summary strip */}
      <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-3">
        <div className="border border-border/60 bg-card/50 p-3">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Book impact (today)
          </div>
          <div
            className={cn(
              "mt-1 font-mono text-2xl",
              pnlClass(bookTotal),
            )}
          >
            {formatUsd(bookTotal, { signed: true })}
          </div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            Σ of (mark − price@publish) × qty across all stories
          </div>
        </div>
        <div className="border border-border/60 bg-card/50 p-3">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Impact stories
          </div>
          <div className="mt-1 font-mono text-2xl">
            {data?.impact.length ?? 0}
          </div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            touching {data?.book_symbols.length ?? 0} symbols in your book
          </div>
        </div>
        <div className="border border-border/60 bg-card/50 p-3">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Book symbols
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {(data?.book_symbols ?? []).map((sym) => (
              <span
                key={sym}
                className="bg-long/10 px-1.5 py-[1px] font-mono text-[11px] text-long"
              >
                {sym}
              </span>
            ))}
            {(data?.book_symbols ?? []).length === 0 ? (
              <span className="text-[11px] text-muted-foreground">
                No open positions
              </span>
            ) : null}
          </div>
        </div>
      </div>

      {/* Filter */}
      <div className="mb-3">
        <Input
          placeholder="Filter by headline, source, or symbol…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-sm"
        />
      </div>

      {/* IMPACT lane */}
      <Card className="mb-4">
        <div className="flex items-center justify-between border-b border-border/60 px-4 py-2">
          <div className="flex items-center gap-2">
            <Briefcase className="h-3.5 w-3.5 text-long" />
            <span className="text-[11px] font-semibold uppercase tracking-wider">
              Impact · your book
            </span>
            <span className="text-[10px] text-muted-foreground">
              sorted by |$ delta|
            </span>
          </div>
          <span className="text-[10px] text-muted-foreground">
            {filtered.impact.length} rows
          </span>
        </div>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="w-12 px-2 py-1 text-right">age</th>
                <th className="w-24 px-2 py-1 text-left">sym · Δ%</th>
                <th className="px-2 py-1 text-left">headline</th>
                <th className="w-28 px-2 py-1 text-right">chart</th>
                <th className="w-24 px-2 py-1 text-right">your Δ $</th>
                <th className="w-24 px-2 py-1 text-right">source</th>
              </tr>
            </thead>
            <tbody>
              {filtered.impact.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    <EmptyState
                      icon={Newspaper}
                      title={
                        query.isLoading
                          ? "Loading…"
                          : (data?.book_symbols.length ?? 0) === 0
                            ? "No positions yet"
                            : "No book-moving news"
                      }
                      description={
                        query.isLoading
                          ? "Fetching the latest headlines from Alpaca…"
                          : (data?.book_symbols.length ?? 0) === 0
                            ? "Open a position and stories touching that symbol will appear here."
                            : "Nothing in the news feed touches your positions right now."
                      }
                      className="m-4"
                    />
                  </td>
                </tr>
              ) : (
                filtered.impact.map((a) => (
                  <ArticleRow key={a.id} article={a} now={now} />
                ))
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {/* UNIVERSE lane */}
      <Card>
        <div className="flex items-center justify-between border-b border-border/60 px-4 py-2">
          <div className="flex items-center gap-2">
            <Newspaper className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-[11px] font-semibold uppercase tracking-wider">
              Universe
            </span>
            <span className="text-[10px] text-muted-foreground">
              time-sorted
            </span>
          </div>
          <span className="text-[10px] text-muted-foreground">
            {filtered.universe.length} rows
          </span>
        </div>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="w-12 px-2 py-1 text-right">age</th>
                <th className="w-24 px-2 py-1 text-left">sym · Δ%</th>
                <th className="px-2 py-1 text-left">headline</th>
                <th className="w-28 px-2 py-1 text-right">chart</th>
                <th className="w-24 px-2 py-1 text-right">your Δ $</th>
                <th className="w-24 px-2 py-1 text-right">source</th>
              </tr>
            </thead>
            <tbody>
              {filtered.universe.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    <EmptyState
                      icon={Newspaper}
                      title="Universe quiet"
                      description={
                        query.isLoading
                          ? "Fetching news…"
                          : "Try clearing the filter or wait for the next refresh."
                      }
                      className="m-4"
                    />
                  </td>
                </tr>
              ) : (
                filtered.universe
                  .slice(0, 50)
                  .map((a: NewsArticle) => (
                    <ArticleRow key={a.id} article={a} now={now} compact />
                  ))
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {query.isError ? (
        <div className="mt-3 border border-short/40 bg-short/10 p-3 text-xs text-short">
          Failed to load news: {String((query.error as Error)?.message ?? "")}
        </div>
      ) : null}

      <p className="mt-4 text-[10px] text-muted-foreground">
        Source: Alpaca /v1beta1/news · sync cadence 30s · sparkline shows
        close prices from publish → now (IEX feed, 1-min). Price-at-publish
        anchors impact math; hover a symbol to open the full{" "}
        <Link href="/positions" className="underline">
          positions
        </Link>{" "}
        view.
      </p>
    </AppShell>
  );
}
