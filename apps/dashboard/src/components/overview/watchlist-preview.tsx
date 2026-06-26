"use client";

import { ArrowRight, Star } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LEDDot } from "@/components/widgets/led-dot";
import { MockBadge } from "@/components/widgets/mock-badge";
import { WatchlistRow, type WatchlistTone } from "@/components/widgets/watchlist-row";
import { directionOf, formatSignedPct, formatSignedUsd } from "@/lib/design-tokens";
import { mockPriceWalk } from "@/lib/mock-data";
import { cn, formatUsd } from "@/lib/utils";

/**
 * WatchlistPreview — compact watchlist card for the home dashboard.
 *
 * Shows the top 6 rows from the mock watchlist.  Mirrors the
 * /watchlist page's row grammar so the user gets a consistent
 * experience.
 */

interface MiniRow {
  symbol: string;
  name: string;
  cap: string;
  last: number;
  change: number;
  changePct: number;
  sparkline: { x: number; y: number }[];
  tone: WatchlistTone;
}

const MINI_BASE: Array<Pick<MiniRow, "symbol" | "name" | "cap" | "tone" | "last">> = [
  { symbol: "AAPL", name: "Apple Inc.", cap: "MEGA", last: 192.34, tone: "long" },
  { symbol: "NVDA", name: "NVIDIA Corp.", cap: "MEGA", last: 845.12, tone: "long" },
  { symbol: "META", name: "Meta Platforms", cap: "MEGA", last: 489.31, tone: "long" },
  { symbol: "TSLA", name: "Tesla Inc.", cap: "MEGA", last: 248.20, tone: "warn" },
  { symbol: "AMD", name: "Advanced Micro Devices", cap: "LARGE", last: 162.45, tone: "long" },
  { symbol: "COIN", name: "Coinbase Global", cap: "MID", last: 218.94, tone: "warn" },
];

function buildMiniWatchlist(): MiniRow[] {
  return MINI_BASE.map((b, i) => {
    const walk = mockPriceWalk({
      seed: 2000 + i * 191,
      count: 30,
      start: b.last * 0.96,
      volatility: 0.014,
      drift: 0.0014,
    });
    const walkLast = walk[walk.length - 1].y;
    const k = b.last / walkLast;
    const sparkline = walk.map((p) => ({ x: p.x, y: p.y * k }));
    const first = sparkline[0].y;
    return {
      ...b,
      change: b.last - first,
      changePct: ((b.last - first) / first) * 100,
      sparkline,
    };
  });
}

export function WatchlistPreview() {
  const rows = useMemo(() => buildMiniWatchlist(), []);
  const adv = rows.filter((r) => r.change > 0).length;
  const decl = rows.filter((r) => r.change < 0).length;
  const advancePct = (adv / rows.length) * 100;

  return (
    <Card className="relative overflow-hidden border-cobalt-soft">
      <span className="pointer-events-none absolute inset-x-0 top-0 h-px bg-cobalt/60" />
      <CardHeader className="flex flex-row items-center justify-between space-y-0 border-b border-hairline bg-background/50 py-2">
        <div className="flex items-center gap-2">
          <Star className="h-3.5 w-3.5 text-cobalt" />
          <CardTitle>Watchlist</CardTitle>
          <MockBadge source="Inline fixture" />
        </div>
        <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          <span className="text-long">{adv} up</span>
          <span>·</span>
          <span className="text-short">{decl} down</span>
          <span>·</span>
          <span className="text-cobalt">{advancePct.toFixed(0)}% adv</span>
          <Link
            href="/watchlist"
            className="ml-2 inline-flex items-center gap-1 border border-hairline px-1.5 py-0.5 text-foreground transition-colors hover:border-cobalt-soft"
          >
            Open
            <ArrowRight className="h-3 w-3" />
          </Link>
        </div>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        <div className="grid h-7 grid-cols-[14px_minmax(0,1fr)_92px_140px_minmax(110px,160px)] items-center gap-3 border-b border-hairline bg-background/40 px-3 font-mono text-[9px] font-bold uppercase tracking-widest text-muted-foreground">
          <span aria-hidden></span>
          <span>Symbol · Name</span>
          <span className="text-right">Last</span>
          <span className="text-right">Δ · %</span>
          <span>Trend</span>
        </div>
        <div>
          {rows.map((r) => (
            <WatchlistRow
              key={r.symbol}
              symbol={r.symbol}
              name={r.name}
              cap={r.cap}
              last={r.last}
              change={r.change}
              changePct={r.changePct}
              sparkline={r.sparkline}
              tone={r.tone}
              isMock
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
