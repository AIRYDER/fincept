"use client";

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  Activity,
  Briefcase,
  Coins,
  DollarSign,
  ScrollText,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { useMemo, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { ConfidenceBar } from "@/components/widgets/confidence-bar";
import { EmptyState } from "@/components/widgets/empty-state";
import { KpiTile } from "@/components/widgets/kpi-tile";
import { PageHeader } from "@/components/widgets/page-header";
import { SideBadge } from "@/components/widgets/side-badge";
import { Sparkline } from "@/components/widgets/sparkline";
import { OrderStatusBadge } from "@/components/widgets/status-badge";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Fill, Prediction, WsFrame } from "@/lib/types";
import { cn, formatNumber, formatUsd, nsToDate, pnlClass } from "@/lib/utils";
import { useFinceptStream } from "@/lib/ws";
import { formatDistanceToNowStrict } from "date-fns";

interface ActivityItem {
  id: string;
  ts: number;
  kind: "prediction" | "fill" | "alert";
  text: React.ReactNode;
}

function asNum(v: string | null | undefined) {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export default function HomePage() {
  const token = useAuth((s) => s.token);

  const { data: positions } = useQuery({
    queryKey: ["positions"],
    queryFn: () => api.positions(token),
    enabled: !!token,
    refetchInterval: 5000,
  });
  const { data: orders } = useQuery({
    queryKey: ["orders", "recent"],
    queryFn: () => api.orders(token, { limit: 50 }),
    enabled: !!token,
    refetchInterval: 5000,
  });
  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.strategies(token),
    enabled: !!token,
    refetchInterval: 30000,
  });

  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [equityHistory, setEquityHistory] = useState<{ x: number; y: number }[]>(
    [],
  );

  // Live stream: predictions, fills, alerts.
  useFinceptStream({
    topics: ["predictions", "fills", "alerts"],
    onFrame: (frame: WsFrame) => {
      const item = toActivity(frame);
      if (item) {
        setActivity((prev) => [item, ...prev].slice(0, 50));
      }
    },
  });

  // Compute KPIs.
  const kpis = useMemo(() => {
    const equity = (positions ?? []).reduce(
      (acc, p) =>
        acc +
        asNum(p.realized_pnl_usd) +
        asNum(p.unrealized_pnl_usd) -
        asNum(p.fees_paid_usd),
      0,
    );
    const unrealized = (positions ?? []).reduce(
      (acc, p) => acc + asNum(p.unrealized_pnl_usd),
      0,
    );
    const open = (positions ?? []).filter((p) => asNum(p.quantity) !== 0).length;
    const fills24h = (orders ?? []).filter((o) => o.status === "filled").length;
    return { equity, unrealized, open, fills24h };
  }, [positions, orders]);

  // Build a "live" sparkline of equity by sampling whenever positions
  // refetch.  This is intentionally cheap - real PnL chart lives on
  // /positions page once we wire the time-series back-end.
  if (
    typeof window !== "undefined" &&
    equityHistory.length === 0 &&
    positions !== undefined
  ) {
    setEquityHistory([{ x: Date.now(), y: kpis.equity }]);
  }

  return (
    <AppShell>
      <PageHeader
        title="Overview"
        description="One pane to see everything: equity, exposure, recent decisions, live signals."
        action={<Badge variant="muted">Auto-refresh · 5s</Badge>}
      />

      {/* KPI row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile
          label="Total equity P&L"
          value={formatUsd(kpis.equity, { signed: true, compact: false })}
          icon={DollarSign}
          delta={kpis.equity}
          sub="Realized + unrealized − fees"
        >
          <Sparkline
            data={equityHistory.length > 1 ? equityHistory : [{ x: 0, y: 0 }, { x: 1, y: kpis.equity }]}
            positive={kpis.equity >= 0}
          />
        </KpiTile>

        <KpiTile
          label="Unrealized P&L"
          value={formatUsd(kpis.unrealized, { signed: true })}
          icon={TrendingUp}
          delta={kpis.unrealized}
          sub="Mark-to-market on open positions"
        />

        <KpiTile
          label="Open positions"
          value={String(kpis.open)}
          icon={Briefcase}
          sub={`${(positions ?? []).length} symbols tracked`}
        />

        <KpiTile
          label="Fills today"
          value={String(kpis.fills24h)}
          icon={ScrollText}
          sub={`${(orders ?? []).length} recent orders`}
        />
      </div>

      {/* Activity + strategy grid */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-3.5 w-3.5" />
              Live activity
            </CardTitle>
            <Badge variant="muted">WebSocket</Badge>
          </CardHeader>
          <CardContent className="px-0 pb-3">
            {activity.length === 0 ? (
              <div className="px-6 pb-3">
                <EmptyState
                  icon={Sparkles}
                  title="Listening for events…"
                  description="Predictions, fills, and alerts will appear here as they happen."
                />
              </div>
            ) : (
              <ScrollArea className="h-72">
                <ul className="divide-y divide-border/50">
                  {activity.map((item) => (
                    <motion.li
                      key={item.id}
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      className="flex items-center gap-3 px-6 py-2.5 text-sm"
                    >
                      <Badge
                        variant={
                          item.kind === "alert"
                            ? "destructive"
                            : item.kind === "fill"
                              ? "long"
                              : "default"
                        }
                        className="w-20 justify-center"
                      >
                        {item.kind.toUpperCase()}
                      </Badge>
                      <span className="flex-1 truncate">{item.text}</span>
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {ago(item.ts)}
                      </span>
                    </motion.li>
                  ))}
                </ul>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2">
              <Coins className="h-3.5 w-3.5" />
              Strategies
            </CardTitle>
            <CardDescription>
              Known strategies in the portfolio store
            </CardDescription>
          </CardHeader>
          <CardContent>
            {(strategies ?? []).length === 0 ? (
              <EmptyState
                icon={Coins}
                title="No strategies yet"
                description="They appear here once an OMS fill creates the first position."
              />
            ) : (
              <ul className="space-y-2">
                {strategies?.map((s) => (
                  <li
                    key={s.strategy_id}
                    className="flex items-center justify-between rounded-md border border-border/40 bg-card/60 px-3 py-2 text-sm"
                  >
                    <span className="font-mono text-xs">{s.strategy_id}</span>
                    <span className="flex items-center gap-2">
                      <Badge variant="muted">{s.position_count} sym</Badge>
                      <Badge variant="long">{s.open_positions} open</Badge>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Top positions strip */}
      <Card className="mt-6">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle>Top positions by |unrealized P&L|</CardTitle>
          <Badge variant="muted">Snapshot</Badge>
        </CardHeader>
        <CardContent className="overflow-x-auto px-0">
          <table className="w-full text-sm">
            <thead className="text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border/40">
                <th className="px-6 py-2 text-left">Symbol</th>
                <th className="px-6 py-2 text-left">Strategy</th>
                <th className="px-6 py-2 text-right">Qty</th>
                <th className="px-6 py-2 text-right">Avg entry</th>
                <th className="px-6 py-2 text-right">Mark</th>
                <th className="px-6 py-2 text-right">Unrealized</th>
                <th className="px-6 py-2 text-right">Realized</th>
              </tr>
            </thead>
            <tbody>
              {(positions ?? [])
                .filter((p) => asNum(p.quantity) !== 0)
                .sort(
                  (a, b) =>
                    Math.abs(asNum(b.unrealized_pnl_usd)) -
                    Math.abs(asNum(a.unrealized_pnl_usd)),
                )
                .slice(0, 8)
                .map((p) => (
                  <tr
                    key={p.position_id}
                    className="border-b border-border/30 last:border-b-0 hover:bg-accent/40"
                  >
                    <td className="px-6 py-2 font-mono">{p.symbol}</td>
                    <td className="px-6 py-2 font-mono text-xs text-muted-foreground">
                      {p.strategy_id}
                    </td>
                    <td className="num px-6 py-2 text-right">
                      {formatNumber(p.quantity, 6)}
                    </td>
                    <td className="num px-6 py-2 text-right text-muted-foreground">
                      {formatUsd(p.avg_entry_price)}
                    </td>
                    <td className="num px-6 py-2 text-right text-muted-foreground">
                      {p.current_mark_price ? formatUsd(p.current_mark_price) : "—"}
                    </td>
                    <td
                      className={cn(
                        "num px-6 py-2 text-right",
                        pnlClass(p.unrealized_pnl_usd),
                      )}
                    >
                      {formatUsd(p.unrealized_pnl_usd, { signed: true })}
                    </td>
                    <td
                      className={cn(
                        "num px-6 py-2 text-right",
                        pnlClass(p.realized_pnl_usd),
                      )}
                    >
                      {formatUsd(p.realized_pnl_usd, { signed: true })}
                    </td>
                  </tr>
                ))}
              {(positions ?? []).filter((p) => asNum(p.quantity) !== 0).length ===
              0 ? (
                <tr>
                  <td colSpan={7}>
                    <EmptyState
                      icon={Briefcase}
                      title="No open positions"
                      description="Your first fill will land here automatically."
                      className="m-4"
                    />
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {/* Live predictions strip */}
      <LivePredictions />
    </AppShell>
  );
}

function LivePredictions() {
  const [latest, setLatest] = useState<Map<string, Prediction>>(new Map());
  useFinceptStream({
    topics: ["predictions"],
    onFrame: (frame) => {
      if (frame.topic !== "predictions") return;
      const p = frame.event.payload;
      setLatest((prev) => {
        const next = new Map(prev);
        next.set(`${p.agent_id}:${p.symbol}`, p);
        return next;
      });
    },
  });
  const rows = Array.from(latest.values()).sort(
    (a, b) => b.ts_event - a.ts_event,
  );
  return (
    <Card className="mt-6">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-primary" />
          Live predictions
        </CardTitle>
        <Badge variant="default">Realtime</Badge>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState
            icon={Sparkles}
            title="Waiting for first prediction"
            description="The gbm_predictor agent posts new direction signals every minute."
          />
        ) : (
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
            {rows.slice(0, 12).map((p) => (
              <motion.div
                key={`${p.agent_id}:${p.symbol}`}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                className="flex items-center gap-3 rounded-md border border-border/40 bg-card/60 px-3 py-2"
              >
                <div className="flex flex-1 flex-col">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm">{p.symbol}</span>
                    <Badge variant="muted">{p.agent_id}</Badge>
                  </div>
                  <ConfidenceBar
                    direction={p.direction}
                    confidence={p.confidence}
                  />
                  <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground">
                    <span
                      className={cn(
                        "font-mono",
                        p.direction >= 0 ? "text-long" : "text-short",
                      )}
                    >
                      dir {p.direction.toFixed(2)}
                    </span>
                    <span className="font-mono">
                      conf {(p.confidence * 100).toFixed(0)}%
                    </span>
                    <span className="font-mono">{ago(p.ts_event)}</span>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
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

function toActivity(frame: WsFrame): ActivityItem | null {
  if (frame.topic === "predictions") {
    const p = frame.event.payload as Prediction;
    return {
      id: `p:${p.agent_id}:${p.symbol}:${p.ts_event}`,
      ts: p.ts_event,
      kind: "prediction",
      text: (
        <span>
          <span className="font-mono">{p.symbol}</span>{" "}
          <span
            className={cn(p.direction >= 0 ? "text-long" : "text-short")}
          >
            {p.direction >= 0 ? "▲" : "▼"} {p.direction.toFixed(2)}
          </span>{" "}
          <span className="text-muted-foreground">
            conf {(p.confidence * 100).toFixed(0)}% · {p.agent_id}
          </span>
        </span>
      ),
    };
  }
  if (frame.topic === "fills") {
    const f = frame.event.payload as Fill;
    return {
      id: `f:${f.fill_id}`,
      ts: f.ts_event,
      kind: "fill",
      text: (
        <span className="flex items-center gap-2">
          <SideBadge side={f.side} />
          <span className="font-mono">{f.symbol}</span>
          <span className="num">{formatNumber(f.quantity, 6)}</span>
          <span className="text-muted-foreground">@ {formatUsd(f.price)}</span>
          <span className="text-muted-foreground">· {f.strategy_id}</span>
        </span>
      ),
    };
  }
  if (frame.topic === "alerts") {
    const a = frame.event.payload;
    return {
      id: `a:${a.alert_id}`,
      ts: a.ts_event,
      kind: "alert",
      text: (
        <span>
          <Badge
            variant={a.severity === "critical" ? "destructive" : "warn"}
            className="mr-2"
          >
            {a.severity.toUpperCase()}
          </Badge>
          {a.code}: {a.message}
        </span>
      ),
    };
  }
  // We don't surface raw position events on the home feed - the table
  // above shows the consolidated view.
  void OrderStatusBadge;
  return null;
}
