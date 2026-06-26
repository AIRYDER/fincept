"use client";

import { useMemo, useState } from "react";
import { ArrowDownAZ, ArrowUpAZ, Filter, Search, Star, StarOff, TrendingUp } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/widgets/empty-state";
import { LEDDot } from "@/components/widgets/led-dot";
import { MockBadge } from "@/components/widgets/mock-badge";
import { PageHeader } from "@/components/widgets/page-header";
import { WatchlistRow, type WatchlistTone } from "@/components/widgets/watchlist-row";
import { mockPriceWalk } from "@/lib/mock-data";
import { cn, formatUsd } from "@/lib/utils";

/**
 * /watchlist — first-class watchlist page.
 *
 * Why this exists:
 *   Every pro trading product has a watchlist.  Fincept's universe
 *   browser on /markets was good, but traders need a stable, dense
 *   table of "the symbols I care about" that doesn't change with
 *   every page reload.  This page is that table.
 *
 * Data source:
 *   Mock-only for now.  Each row carries a MOCK chip and a single
 *   page-level MOCK chip in the header.  When the watchlist API
 *   lands, replace `buildMockWatchlist()` with the real call.
 */

interface WatchRow {
  symbol: string;
  name: string;
  cap: string;
  last: number;
  change: number;
  changePct: number;
  volume: number;
  sparkline: { x: number; y: number }[];
  tone: WatchlistTone;
  pinned: boolean;
}

const BASE: Array<Pick<WatchRow, "symbol" | "name" | "cap" | "tone" | "last" | "pinned">> = [
  { symbol: "AAPL", name: "Apple Inc.", cap: "MEGA", last: 192.34, tone: "long", pinned: true },
  { symbol: "NVDA", name: "NVIDIA Corp.", cap: "MEGA", last: 845.12, tone: "long", pinned: true },
  { symbol: "MSFT", name: "Microsoft Corp.", cap: "MEGA", last: 412.78, tone: "long", pinned: true },
  { symbol: "GOOGL", name: "Alphabet Inc.", cap: "MEGA", last: 168.92, tone: "info", pinned: false },
  { symbol: "META", name: "Meta Platforms", cap: "MEGA", last: 489.31, tone: "long", pinned: true },
  { symbol: "AMZN", name: "Amazon.com Inc.", cap: "MEGA", last: 178.65, tone: "info", pinned: false },
  { symbol: "TSLA", name: "Tesla Inc.", cap: "MEGA", last: 248.20, tone: "warn", pinned: true },
  { symbol: "AMD", name: "Advanced Micro Devices", cap: "LARGE", last: 162.45, tone: "long", pinned: false },
  { symbol: "AVGO", name: "Broadcom Inc.", cap: "MEGA", last: 1324.10, tone: "long", pinned: false },
  { symbol: "JPM", name: "JPMorgan Chase", cap: "LARGE", last: 196.78, tone: "info", pinned: false },
  { symbol: "V", name: "Visa Inc.", cap: "MEGA", last: 271.45, tone: "long", pinned: false },
  { symbol: "XOM", name: "Exxon Mobil", cap: "LARGE", last: 117.83, tone: "short", pinned: false },
  { symbol: "COIN", name: "Coinbase Global", cap: "MID", last: 218.94, tone: "warn", pinned: true },
  { symbol: "PLTR", name: "Palantir Technologies", cap: "MID", last: 24.18, tone: "long", pinned: false },
  { symbol: "SMCI", name: "Super Micro Computer", cap: "MID", last: 41.27, tone: "short", pinned: false },
  { symbol: "ARM", name: "Arm Holdings", cap: "LARGE", last: 119.46, tone: "info", pinned: false },
  { symbol: "MSTR", name: "MicroStrategy", cap: "MID", last: 1418.20, tone: "long", pinned: false },
  { symbol: "SPY", name: "S&P 500 ETF", cap: "ETF", last: 547.32, tone: "info", pinned: true },
  { symbol: "QQQ", name: "Invesco QQQ Trust", cap: "ETF", last: 482.10, tone: "info", pinned: false },
  { symbol: "IBIT", name: "iShares Bitcoin Trust", cap: "ETF", last: 36.45, tone: "long", pinned: false },
];

function buildMockWatchlist(): WatchRow[] {
  return BASE.map((b, i) => {
    // Deterministic walk with light volatility; the first point of the
    // walk is the "previous close", the last is `b.last`.
    const walk = mockPriceWalk({
      seed: 1000 + i * 137,
      count: 30,
      start: b.last * 0.96,
      volatility: 0.012,
      drift: 0.0015,
    });
    // Normalize so the last point lands on b.last exactly.
    const walkLast = walk[walk.length - 1].y;
    const k = b.last / walkLast;
    const sparkline = walk.map((p) => ({ x: p.x, y: p.y * k }));
    const first = sparkline[0].y;
    const change = b.last - first;
    const changePct = first !== 0 ? (change / first) * 100 : 0;
    const volume = Math.round(800_000 + Math.abs(Math.sin(i * 1.3)) * 12_000_000);
    return {
      ...b,
      change,
      changePct,
      sparkline,
      volume,
    };
  });
}

type SortKey = "symbol" | "last" | "change" | "changePct" | "volume";
type SortDir = "asc" | "desc";

export default function WatchlistPage() {
  const all = useMemo(() => buildMockWatchlist(), []);
  const [filter, setFilter] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("changePct");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const visible = useMemo(() => {
    const f = filter.toLowerCase();
    let rows = all.filter((r) => {
      if (pinnedOnly && !r.pinned) return false;
      if (!f) return true;
      return (
        r.symbol.toLowerCase().includes(f) ||
        r.name.toLowerCase().includes(f) ||
        r.cap.toLowerCase().includes(f)
      );
    });
    rows = rows.slice().sort((a, b) => {
      const av = a[sortKey] as number | string;
      const bv = b[sortKey] as number | string;
      const cmp = typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv));
      return sortDir === "asc" ? cmp : -cmp;
    });
    return rows;
  }, [all, filter, pinnedOnly, sortKey, sortDir]);

  // Summary stats
  const stats = useMemo(() => {
    const total = all.length;
    const pinned = all.filter((r) => r.pinned).length;
    const up = all.filter((r) => r.change > 0).length;
    const down = all.filter((r) => r.change < 0).length;
    const flat = total - up - down;
    const advPct = total > 0 ? (up / total) * 100 : 0;
    return { total, pinned, up, down, flat, advPct };
  }, [all]);

  return (
    <AppShell>
      <PageHeader
        title="Watchlist"
        description="A dense, scannable table of symbols you care about. Pin favorites, sort by any column, click a row to drill into the symbol detail view."
        action={
          <div className="flex items-center gap-2">
            <MockBadge source="Inline fixture" />
            <Badge variant="muted">{stats.total} symbols</Badge>
            <Badge variant="long">{stats.up} up</Badge>
            <Badge variant="short">{stats.down} down</Badge>
          </div>
        }
      />

      {/* Summary band */}
      <div className="glass mb-3 grid grid-cols-2 gap-0 md:grid-cols-5">
        <StatCell label="Tracked" value={String(stats.total)} tone="info" />
        <StatCell label="Pinned" value={String(stats.pinned)} tone="warn" />
        <StatCell
          label="Advancing"
          value={`${stats.up} · ${stats.advPct.toFixed(0)}%`}
          tone="long"
        />
        <StatCell label="Declining" value={String(stats.down)} tone="short" />
        <StatCell label="Flat" value={String(stats.flat)} tone="muted" />
      </div>

      {/* Toolbar */}
      <div className="mb-2 flex flex-wrap items-center gap-2 border border-hairline bg-background/40 p-2">
        <div className="relative min-w-[14rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter by symbol, name, or cap tier…"
            className="h-8 pl-8 font-mono text-xs"
          />
        </div>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant={pinnedOnly ? "default" : "outline"}
            size="sm"
            onClick={() => setPinnedOnly((v) => !v)}
            className="gap-1.5"
          >
            {pinnedOnly ? (
              <Star className="h-3 w-3 fill-current" />
            ) : (
              <StarOff className="h-3 w-3" />
            )}
            Pinned
          </Button>
        </div>
        <div className="ml-auto flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          <Filter className="h-3 w-3" />
          Sort
        </div>
        <div className="flex items-center gap-1">
          {(["symbol", "last", "changePct", "volume"] as const).map((k) => (
            <SortChip
              key={k}
              active={sortKey === k}
              dir={sortKey === k ? sortDir : "desc"}
              label={
                k === "symbol" ? "Symbol"
                : k === "last" ? "Last"
                : k === "changePct" ? "Change %"
                : "Volume"
              }
              onClick={() => {
                if (sortKey === k) {
                  setSortDir((d) => (d === "asc" ? "desc" : "asc"));
                } else {
                  setSortKey(k);
                  setSortDir(k === "symbol" ? "asc" : "desc");
                }
              }}
            />
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="glass overflow-hidden">
        <div className="grid h-8 grid-cols-[14px_minmax(0,1fr)_92px_140px_minmax(110px,160px)] items-center gap-3 border-b border-hairline bg-background/50 px-3 font-mono text-[9px] font-bold uppercase tracking-widest text-muted-foreground">
          <span aria-hidden></span>
          <span>Symbol · Name</span>
          <span className="text-right">Last</span>
          <span className="text-right">Δ · %</span>
          <span>Trend</span>
        </div>
        {visible.length === 0 ? (
          <div className="p-3">
            <EmptyState
              icon={TrendingUp}
              title="No symbols match"
              description={
                filter || pinnedOnly
                  ? "Adjust the filter or clear the pinned toggle."
                  : "Add symbols from the universe browser."
              }
            />
          </div>
        ) : (
          <div role="rowgroup">
            {visible.map((row) => (
              <WatchlistRow
                key={row.symbol}
                symbol={row.symbol}
                name={row.name}
                cap={row.cap}
                last={row.last}
                change={row.change}
                changePct={row.changePct}
                volume={row.volume}
                sparkline={row.sparkline}
                tone={row.tone}
                isMock
              />
            ))}
          </div>
        )}
      </div>

      <p className="mt-3 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
        MOCK: row data is an inline fixture; replace with watchlist API
        when the endpoint ships.  Each row is a Link to /symbol/{`{symbol}`}.
      </p>
    </AppShell>
  );
}

function StatCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: WatchlistTone;
}) {
  const colorClass =
    tone === "long" ? "text-long"
    : tone === "short" ? "text-short"
    : tone === "warn" ? "text-warn"
    : tone === "info" ? "text-cobalt"
    : "text-muted-foreground";
  return (
    <div className="flex flex-col gap-0.5 border-r border-hairline px-4 py-2 last:border-r-0">
      <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <span className={cn("font-mono text-lg font-semibold tabular-nums", colorClass)}>
        {value}
      </span>
    </div>
  );
}

function SortChip({
  label,
  active,
  dir,
  onClick,
}: {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex h-6 items-center gap-1 border px-2 font-mono text-[10px] font-bold uppercase tracking-widest transition-colors",
        active
          ? "border-cobalt bg-cobalt/10 text-cobalt"
          : "border-hairline bg-background/40 text-muted-foreground hover:border-cobalt-soft hover:text-foreground",
      )}
    >
      {label}
      {active ? (
        dir === "asc" ? (
          <ArrowDownAZ className="h-3 w-3" />
        ) : (
          <ArrowUpAZ className="h-3 w-3" />
        )
      ) : null}
    </button>
  );
}
