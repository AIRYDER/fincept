"use client";

import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { SideBadge } from "@/components/widgets/side-badge";
import { OrderStatusBadge } from "@/components/widgets/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { OrderStatus } from "@/lib/types";
import { cn, formatNumber, formatUsd, nsToDate } from "@/lib/utils";

const STATUS_FILTERS: Array<OrderStatus | "all"> = [
  "all",
  "filled",
  "new",
  "pending_new",
  "rejected",
  "cancelled",
];

export default function OrdersPage() {
  const token = useAuth((s) => s.token);
  const [status, setStatus] = useState<OrderStatus | "all">("all");
  const [filter, setFilter] = useState("");

  const { data, isFetching } = useQuery({
    queryKey: ["orders", status],
    queryFn: () =>
      api.orders(token, {
        status: status === "all" ? undefined : status,
        limit: 200,
      }),
    enabled: !!token,
    refetchInterval: 5000,
  });

  const rows = (data ?? []).filter((o) => {
    if (!filter) return true;
    const f = filter.toLowerCase();
    return (
      o.symbol.toLowerCase().includes(f) ||
      o.strategy_id.toLowerCase().includes(f) ||
      o.order_id.toLowerCase().includes(f)
    );
  });

  return (
    <AppShell>
      <PageHeader
        title="Orders"
        description="Latest snapshot per order_id, materialised from the audit_log table.  Newest first."
        action={
          <Badge variant={isFetching ? "warn" : "muted"}>
            {isFetching ? "Updating…" : "Auto-refresh · 5s"}
          </Badge>
        }
      />

      <div className="mb-3 flex flex-wrap items-center gap-2">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s}
            onClick={() => setStatus(s)}
            className={cn(
              "rounded-md border border-border/60 bg-background/40 px-3 py-1.5 text-xs uppercase tracking-wider transition-colors",
              status === s
                ? "border-primary/60 bg-primary/15 text-primary"
                : "text-muted-foreground hover:bg-accent",
            )}
          >
            {s.replace("_", " ")}
          </button>
        ))}
        <Input
          placeholder="Filter by symbol, strategy, or order id…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="ml-auto max-w-sm"
        />
      </div>

      <Card>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="px-4 py-2 text-left">When</th>
                <th className="px-4 py-2 text-left">Symbol</th>
                <th className="px-4 py-2 text-left">Side</th>
                <th className="px-4 py-2 text-left">Type</th>
                <th className="px-4 py-2 text-right">Quantity</th>
                <th className="px-4 py-2 text-right">Filled</th>
                <th className="px-4 py-2 text-right">Avg fill</th>
                <th className="px-4 py-2 text-left">Status</th>
                <th className="px-4 py-2 text-left">Strategy</th>
                <th className="px-4 py-2 text-left">Venue</th>
                <th className="px-4 py-2 text-left">Order id</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={11}>
                    <EmptyState
                      icon={ScrollText}
                      title="No orders match"
                      description={
                        filter || status !== "all"
                          ? "Try a different filter or clear the search."
                          : "Once the orchestrator emits an OrderIntent, the OMS records it here."
                      }
                      className="m-4"
                    />
                  </td>
                </tr>
              ) : (
                rows.map((o) => (
                  <tr
                    key={o.order_id}
                    className="border-b border-border/30 hover:bg-accent/30"
                  >
                    <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                      {nsToDate(o.updated_at)?.toISOString().slice(11, 19) ?? "—"}
                    </td>
                    <td className="px-4 py-2 font-mono">{o.symbol}</td>
                    <td className="px-4 py-2">
                      <SideBadge side={o.side} />
                    </td>
                    <td className="px-4 py-2 font-mono text-xs uppercase text-muted-foreground">
                      {o.order_type}
                    </td>
                    <td className="num px-4 py-2 text-right">
                      {formatNumber(o.quantity, 6)}
                    </td>
                    <td className="num px-4 py-2 text-right">
                      {formatNumber(o.filled_qty, 6)}
                    </td>
                    <td className="num px-4 py-2 text-right text-muted-foreground">
                      {o.avg_fill_price ? formatUsd(o.avg_fill_price) : "—"}
                    </td>
                    <td className="px-4 py-2">
                      <OrderStatusBadge status={o.status} />
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                      {o.strategy_id}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs uppercase">
                      {o.venue}
                    </td>
                    <td className="px-4 py-2 font-mono text-[10px] text-muted-foreground">
                      {o.order_id.slice(0, 10)}…
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </AppShell>
  );
}
