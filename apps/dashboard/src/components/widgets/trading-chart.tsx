"use client";

import { useMemo, useState } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { cn, formatUsd } from "@/lib/utils";

/**
 * TradingChart — area chart with volume bars, range chips, and a
 * last-price pin.
 *
 * Built for the /symbol/[symbol] page but reusable everywhere
 * (portfolio-builder, news-impact-lab, watchlist mini, etc.).
 *
 * Design:
 *   - Cobalt area gradient (long-direction feel by default).
 *   - Orange/short variant flips the gradient.
 *   - Volume bars at the bottom 25% — same x-axis.
 *   - Range chips (1D/1W/1M/3M/ALL) — purely visual state, parent
 *     controls the data and re-fetches on change.
 *   - Last-price horizontal pin with the latest close.
 *   - Crosshair (custom Recharts tooltip).
 *
 * Mock-friendly: pass `isMock` to add a MOCK chip in the corner.
 */

export type ChartRange = "1D" | "1W" | "1M" | "3M" | "ALL";

export interface TradingChartPoint {
  /** X-axis value — typically a unix millisecond timestamp. */
  x: number;
  /** Close price. */
  close: number;
  /** Optional high/low to drive the price pin / tooltip. */
  high?: number;
  low?: number;
  /** Optional volume for the volume bar. */
  volume?: number;
}

export interface TradingChartProps {
  data: TradingChartPoint[];
  /** Direction bias — controls the area gradient color. */
  direction?: "up" | "down" | "flat";
  /** Range chips; pass the active range and the onChange handler. */
  range?: ChartRange;
  onRangeChange?: (range: ChartRange) => void;
  /** Available ranges to render as chips. */
  ranges?: ChartRange[];
  /** Symbol label, displayed in the corner. */
  symbol?: string;
  /** Latest price pin label (e.g. "MARK 194.21"). */
  lastLabel?: string;
  /** Disclose mock data with a MOCK chip. */
  isMock?: boolean;
  /** Pixel height of the chart canvas. */
  height?: number;
  className?: string;
}

const RANGES: ChartRange[] = ["1D", "1W", "1M", "3M", "ALL"];

export function TradingChart({
  data,
  direction = "up",
  range = "1M",
  onRangeChange,
  ranges = RANGES,
  symbol,
  lastLabel,
  isMock = false,
  height = 320,
  className,
}: TradingChartProps) {
  const [hover, setHover] = useState<TradingChartPoint | null>(null);

  const { last, change, changePct, min, max } = useMemo(() => {
    if (data.length === 0) {
      return { last: 0, change: 0, changePct: 0, min: 0, max: 0 };
    }
    const first = data[0].close;
    const latest = data[data.length - 1];
    const change = latest.close - first;
    const changePct = first !== 0 ? (change / first) * 100 : 0;
    const prices = data.map((d) => d.close);
    return {
      last: latest.close,
      change,
      changePct,
      min: Math.min(...prices),
      max: Math.max(...prices),
    };
  }, [data]);

  const isUp = direction === "up" || (direction === "flat" && change >= 0);
  const areaId = "tradingchart-area-gradient";
  const areaColor = isUp ? "hsl(218 100% 60%)" : "hsl(24 100% 56%)"; // cobalt : orange
  const areaColorSoft = isUp ? "hsl(218 100% 70%)" : "hsl(30 100% 68%)";
  const lineColor = isUp ? "hsl(218 100% 60%)" : "hsl(24 100% 56%)";
  const dotColor = isUp ? "hsl(140 92% 52%)" : "hsl(0 88% 62%)";

  const display = hover ?? data[data.length - 1] ?? null;

  return (
    <div
      className={cn(
        "glass relative flex flex-col gap-2 p-3",
        className,
      )}
      style={{ minHeight: height + 64 }}
    >
      {/* Header row: symbol + price + change + range chips */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-end gap-3">
          {symbol ? (
            <div>
              <div className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
                Symbol
              </div>
              <div className="font-mono text-2xl font-semibold tracking-tight text-foreground">
                {symbol}
              </div>
            </div>
          ) : null}
          <div>
            <div className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground">
              {lastLabel ?? "Last"}
            </div>
            <div className="flex items-baseline gap-2">
              <div className="font-mono text-xl font-semibold tabular-nums text-foreground">
                {formatUsd(last)}
              </div>
              <div
                className={cn(
                  "font-mono text-xs font-semibold tabular-nums",
                  change > 0
                    ? "text-long"
                    : change < 0
                      ? "text-short"
                      : "text-muted-foreground",
                )}
              >
                {change >= 0 ? "+" : ""}
                {change.toFixed(2)} ({changePct >= 0 ? "+" : ""}
                {changePct.toFixed(2)}%)
              </div>
            </div>
          </div>
          {isMock ? (
            <span className="border border-dashed border-warn/60 bg-warn/10 px-1.5 py-[1px] font-mono text-[9px] font-bold uppercase tracking-widest text-warn">
              MOCK
            </span>
          ) : null}
        </div>
        {onRangeChange ? (
          <div className="flex items-center gap-1">
            {ranges.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => onRangeChange(r)}
                className={cn(
                  "h-6 border px-2 font-mono text-[10px] font-bold uppercase tracking-widest transition-colors",
                  r === range
                    ? "border-cobalt bg-cobalt/10 text-cobalt"
                    : "border-hairline bg-background/40 text-muted-foreground hover:border-cobalt-soft hover:text-foreground",
                )}
              >
                {r}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      {/* The chart canvas */}
      <div className="relative" style={{ height }}>
        {data.length === 0 ? (
          <div className="flex h-full items-center justify-center border border-dashed border-hairline bg-background/30 text-xs text-muted-foreground">
            No bars in this range.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart
              data={data}
              margin={{ top: 8, right: 12, bottom: 8, left: 8 }}
              onMouseMove={(state) => {
                if (
                  state && typeof state === "object" &&
                  "activePayload" in state &&
                  Array.isArray((state as { activePayload?: unknown[] }).activePayload) &&
                  (state as { activePayload: { payload: TradingChartPoint }[] }).activePayload
                    .length > 0
                ) {
                  setHover(
                    (state as { activePayload: { payload: TradingChartPoint }[] })
                      .activePayload[0].payload,
                  );
                }
              }}
              onMouseLeave={() => setHover(null)}
            >
              <defs>
                <linearGradient id={areaId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={areaColor} stopOpacity={0.55} />
                  <stop offset="60%" stopColor={areaColorSoft} stopOpacity={0.18} />
                  <stop offset="100%" stopColor={areaColorSoft} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                stroke="hsl(220 10% 22% / 0.55)"
                strokeDasharray="2 4"
                vertical={false}
              />
              <XAxis
                dataKey="x"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={fmtTick(range)}
                stroke="hsl(220 8% 58%)"
                fontSize={10}
                axisLine={false}
                tickLine={false}
                minTickGap={48}
              />
              <YAxis
                yAxisId="price"
                domain={[
                  (dataMin: number) => Math.floor((dataMin - (max - min) * 0.05) * 100) / 100,
                  (dataMax: number) => Math.ceil((dataMax + (max - min) * 0.05) * 100) / 100,
                ]}
                stroke="hsl(220 8% 58%)"
                fontSize={10}
                axisLine={false}
                tickLine={false}
                width={56}
                tickFormatter={(v: number) =>
                  v.toLocaleString("en-US", { maximumFractionDigits: 2 })
                }
                orientation="right"
              />
              <YAxis
                yAxisId="volume"
                orientation="left"
                domain={[0, "dataMax"]}
                hide
              />
              <Tooltip
                cursor={{ stroke: "hsl(218 100% 60%)", strokeWidth: 1, strokeDasharray: "2 3" }}
                content={() => null}
              />
              <Bar
                yAxisId="volume"
                dataKey="volume"
                fill="hsl(218 100% 60% / 0.10)"
                isAnimationActive={false}
                maxBarSize={2}
              />
              <Area
                yAxisId="price"
                type="monotone"
                dataKey="close"
                stroke={lineColor}
                strokeWidth={1.5}
                fill={`url(#${areaId})`}
                isAnimationActive={false}
                activeDot={{ r: 3, fill: dotColor, stroke: "hsl(0 0% 0%)" }}
              />
              <Line
                yAxisId="price"
                type="monotone"
                dataKey="close"
                stroke="transparent"
                dot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}

        {/* Last-price horizontal pin */}
        {display ? (
          <div
            className={cn(
              "pointer-events-none absolute right-2 z-10 flex items-center gap-1 border px-1.5 py-[1px] font-mono text-[10px] font-bold tabular-nums",
              change > 0
                ? "border-long/40 bg-long/10 text-long"
                : change < 0
                  ? "border-short/40 bg-short/10 text-short"
                  : "border-hairline bg-muted text-muted-foreground",
            )}
            style={{
              top: `calc(${pricePct(display.close, min, max)}% - 8px)`,
            }}
          >
            <span className="h-1 w-1 rounded-full bg-current" />
            {formatUsd(display.close)}
          </div>
        ) : null}

        {/* Scanline overlay for the instrument-panel feel */}
        <div className="pointer-events-none absolute inset-0 scanlines" />
      </div>

      {/* Footer: x-axis tooltip-style summary */}
      {display ? (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline pt-2 text-[10px] text-muted-foreground">
          <div className="flex items-center gap-3 font-mono uppercase tracking-widest">
            <span>
              <span className="text-muted-foreground/70">DATE </span>
              <span className="text-foreground">
                {fmtDate(display.x, range)}
              </span>
            </span>
            <span>
              <span className="text-muted-foreground/70">CLOSE </span>
              <span className="text-foreground">{formatUsd(display.close)}</span>
            </span>
            {display.high !== undefined ? (
              <span>
                <span className="text-muted-foreground/70">HI </span>
                <span className="text-long">{formatUsd(display.high)}</span>
              </span>
            ) : null}
            {display.low !== undefined ? (
              <span>
                <span className="text-muted-foreground/70">LO </span>
                <span className="text-short">{formatUsd(display.low)}</span>
              </span>
            ) : null}
            {display.volume !== undefined ? (
              <span>
                <span className="text-muted-foreground/70">VOL </span>
                <span className="text-foreground">
                  {Intl.NumberFormat("en-US", { notation: "compact" }).format(display.volume)}
                </span>
              </span>
            ) : null}
          </div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
            drag to inspect · mock unless flagged
          </div>
        </div>
      ) : null}
    </div>
  );
}

function pricePct(close: number, min: number, max: number) {
  if (max === min) return 50;
  const padded = (max - min) * 0.05;
  const lo = min - padded;
  const hi = max + padded;
  // Invert because 0% is at the top of the chart canvas.
  const p = (close - lo) / (hi - lo);
  return (1 - p) * 100;
}

function fmtTick(range: ChartRange): (v: number) => string {
  return (v: number) => {
    const d = new Date(v);
    if (range === "1D") {
      return d.toISOString().slice(11, 16);
    }
    if (range === "1W" || range === "1M") {
      return d.toISOString().slice(5, 10);
    }
    return d.toISOString().slice(0, 7);
  };
}

function fmtDate(x: number, range: ChartRange): string {
  const d = new Date(x);
  if (range === "1D") return d.toISOString().slice(0, 16).replace("T", " ");
  if (range === "1W" || range === "1M") return d.toISOString().slice(0, 10);
  return d.toISOString().slice(0, 7);
}
