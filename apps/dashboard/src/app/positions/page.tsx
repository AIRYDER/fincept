"use client";

import { useQuery } from "@tanstack/react-query";
import { Briefcase } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Position, WsFrame } from "@/lib/types";
import { cn, formatNumber, formatUsd, pnlClass } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";

function asNum(v: string | null | undefined) {
  if (v == null) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

/** key = strategy_id + ":" + symbol */
function posKey(p: { strategy_id: string; symbol: string }) {
  return `${p.strategy_id}:${p.symbol}`;
}

export default function PositionsPage() {
  const token = useAuth((s) => s.token);
  const [filter, setFilter] = useState("");
  const [includeFlat, setIncludeFlat] = useState(false);
  const [pulseSet, setPulseSet] = useState<Set<string>>(new Set());

  const { data: initial, refetch } = useQuery({
    queryKey: ["positions", "all", includeFlat],
    queryFn: () => api.positions(token, includeFlat),
    enabled: !!token,
    refetchInterval: 5000,
  });

  // Keep a live map keyed by (strategy_id, symbol).  WS pushes upsert.
  const [byKey, setByKey] = useState<Map<string, Position>>(new Map());
  useEffect(() => {
    if (!initial) return;
    const m = new Map<string, Position>();
    for (const p of initial) m.set(posKey(p), p);
    setByKey(m);
  }, [initial]);

  const onFrame = useCallback(
    (frame: WsFrame) => {
      if (frame.topic !== "positions") return;
      const pos = frame.event.payload;
      setByKey((prev) => {
        const next = new Map(prev);
        next.set(posKey(pos), pos);
        return next;
      });
      const k = posKey(pos);
      setPulseSet((prev) => {
        const next = new Set(prev);
        next.add(k);
        return next;
      });
      // Clear the pulse class after the animation runs.
      setTimeout(() => {
        setPulseSet((prev) => {
          const next = new Set(prev);
          next.delete(k);
          return next;
        });
      }, 800);
    },
    [],
  );

  useFinceptStream({ topics: ["positions"], onFrame });

  const rows = useMemo(() => {
    const all = Array.from(byKey.values());
    const filtered = all.filter((p) => {
      if (!includeFlat && asNum(p.quantity) === 0) return false;
      if (!filter) return true;
      const f = filter.toLowerCase();
      return (
        p.symbol.toLowerCase().includes(f) ||
        p.strategy_id.toLowerCase().includes(f)
      );
    });
    return filtered.sort(
      (a, b) =>
        Math.abs(asNum(b.unrealized_pnl_usd)) -
        Math.abs(asNum(a.unrealized_pnl_usd)),
    );
  }, [byKey, filter, includeFlat]);

  const totals = useMemo(() => {
    return rows.reduce(
      (acc, p) => {
        acc.realized += asNum(p.realized_pnl_usd);
        acc.unrealized += asNum(p.unrealized_pnl_usd);
        acc.fees += asNum(p.fees_paid_usd);
        acc.gross += Math.abs(asNum(p.quantity)) * asNum(p.current_mark_price ?? p.avg_entry_price);
        return acc;
      },
      { realized: 0, unrealized: 0, fees: 0, gross: 0 },
    );
  }, [rows]);

  return (
    <AppShell>
      <PageHeader
        title="Positions"
        description="Live positions across all strategies.  Updates push at 10 Hz over WebSocket; unrealized P&L tracks the latest mark price from md.trades."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="muted">{rows.length} rows</Badge>
            <button
              onClick={() => setIncludeFlat((v) => !v)}
              className="rounded-md border border-border/60 bg-background/40 px-3 py-1.5 text-xs"
            >
              {includeFlat ? "Hide flat" : "Show flat"}
            </button>
            <button
              onClick={() => refetch()}
              className="rounded-md border border-border/60 bg-background/40 px-3 py-1.5 text-xs"
            >
              Refresh
            </button>
          </div>
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
        <SummaryTile
          label="Realized"
          value={formatUsd(totals.realized, { signed: true })}
          colorClass={pnlClass(totals.realized)}
        />
        <SummaryTile
          label="Unrealized"
          value={formatUsd(totals.unrealized, { signed: true })}
          colorClass={pnlClass(totals.unrealized)}
        />
        <SummaryTile
          label="Fees paid"
          value={formatUsd(-totals.fees, { signed: true })}
          colorClass="text-warn"
        />
        <SummaryTile
          label="Gross exposure"
          value={formatUsd(totals.gross, { compact: true })}
          colorClass="text-foreground"
        />
      </div>

      <div className="mb-3">
        <Input
          placeholder="Filter by symbol or strategy_id…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-sm"
        />
      </div>

      <Card>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10 bg-card text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="px-4 py-2 text-left">Symbol</th>
                <th className="px-4 py-2 text-left">Strategy</th>
                <th className="px-4 py-2 text-right">Qty</th>
                <th className="px-4 py-2 text-right">Avg entry</th>
                <th className="px-4 py-2 text-right">Mark</th>
                <th className="px-4 py-2 text-right">Notional</th>
                <th className="px-4 py-2 text-right">Unrealized</th>
                <th className="px-4 py-2 text-right">Realized</th>
                <th className="px-4 py-2 text-right">Fees</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={9}>
                    <EmptyState
                      icon={Briefcase}
                      title="No positions match"
                      description={
                        filter
                          ? "Try a different filter or clear the input."
                          : "When the OMS records a fill, the position will appear here."
                      }
                      className="m-4"
                    />
                  </td>
                </tr>
              ) : (
                rows.map((p) => {
                  const qty = asNum(p.quantity);
                  const mark = asNum(
                    p.current_mark_price ?? p.avg_entry_price,
                  );
                  const notional = qty * mark;
                  const k = posKey(p);
                  const pulse = pulseSet.has(k);
                  return (
                    <tr
                      key={p.position_id}
                      className={cn(
                        "border-b border-border/30 hover:bg-accent/30",
                        pulse && "pulse-update",
                      )}
                    >
                      <td className="px-4 py-2 font-mono">
                        <span className="flex items-center gap-2">
                          {p.symbol}
                          {qty > 0 ? (
                            <Badge variant="long">LONG</Badge>
                          ) : qty < 0 ? (
                            <Badge variant="short">SHORT</Badge>
                          ) : (
                            <Badge variant="muted">FLAT</Badge>
                          )}
                        </span>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                        {p.strategy_id}
                      </td>
                      <td className="num px-4 py-2 text-right">
                        {formatNumber(p.quantity, 6)}
                      </td>
                      <td className="num px-4 py-2 text-right text-muted-foreground">
                        {formatUsd(p.avg_entry_price)}
                      </td>
                      <td className="num px-4 py-2 text-right">
                        {p.current_mark_price
                          ? formatUsd(p.current_mark_price)
                          : "—"}
                      </td>
                      <td className="num px-4 py-2 text-right text-muted-foreground">
                        {formatUsd(notional, { compact: true })}
                      </td>
                      <td
                        className={cn(
                          "num px-4 py-2 text-right",
                          pnlClass(p.unrealized_pnl_usd),
                        )}
                      >
                        {formatUsd(p.unrealized_pnl_usd, { signed: true })}
                      </td>
                      <td
                        className={cn(
                          "num px-4 py-2 text-right",
                          pnlClass(p.realized_pnl_usd),
                        )}
                      >
                        {formatUsd(p.realized_pnl_usd, { signed: true })}
                      </td>
                      <td className="num px-4 py-2 text-right text-warn">
                        {formatUsd(-asNum(p.fees_paid_usd), { signed: true })}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </AppShell>
  );
}

function SummaryTile({
  label,
  value,
  colorClass,
}: {
  label: string;
  value: string;
  colorClass: string;
}) {
  return (
    <div className="rounded-lg border border-border/40 bg-card/40 p-3">
      <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className={cn("num mt-1 text-xl font-semibold", colorClass)}>
        {value}
      </div>
    </div>
  );
}
