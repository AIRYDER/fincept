"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, Briefcase } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatUsd, pnlClass } from "@/lib/utils";

function asNum(v: string | null | undefined) {
  if (v == null) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export default function StrategiesPage() {
  const token = useAuth((s) => s.token);

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.strategies(token),
    enabled: !!token,
    refetchInterval: 10000,
  });
  const { data: positions } = useQuery({
    queryKey: ["positions", "all"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 10000,
  });

  const positionsByStrategy = new Map<string, typeof positions>();
  for (const p of positions ?? []) {
    if (!positionsByStrategy.has(p.strategy_id)) {
      positionsByStrategy.set(p.strategy_id, []);
    }
    positionsByStrategy.get(p.strategy_id)!.push(p);
  }

  return (
    <AppShell>
      <PageHeader
        title="Strategies"
        description="Each strategy is a logical owner of positions and orders.  v1 surfaces the registry; start/stop control lands when the strategy host service ships."
        action={
          <Badge variant="muted">{(strategies ?? []).length} known</Badge>
        }
      />

      {(strategies ?? []).length === 0 ? (
        <EmptyState
          icon={Bot}
          title="No strategies registered yet"
          description="The portfolio service registers a strategy id automatically when the first fill lands."
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {strategies?.map((s) => {
            const pos = positionsByStrategy.get(s.strategy_id) ?? [];
            const realized = pos.reduce(
              (acc, p) => acc + asNum(p.realized_pnl_usd),
              0,
            );
            const unrealized = pos.reduce(
              (acc, p) => acc + asNum(p.unrealized_pnl_usd),
              0,
            );
            const fees = pos.reduce(
              (acc, p) => acc + asNum(p.fees_paid_usd),
              0,
            );
            const total = realized + unrealized - fees;
            return (
              <Card key={s.strategy_id} className="relative overflow-hidden">
                <CardHeader className="pb-3">
                  <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
                    <Bot className="h-4 w-4 text-primary" />
                    <span className="font-mono text-sm">{s.strategy_id}</span>
                  </CardTitle>
                  <CardDescription>
                    <Badge variant="muted" className="mr-2">
                      <Briefcase className="mr-1 h-3 w-3" />
                      {s.position_count} sym
                    </Badge>
                    <Badge variant="long">{s.open_positions} open</Badge>
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="grid grid-cols-3 gap-2">
                    <Stat label="Realized" value={realized} />
                    <Stat label="Unrealized" value={unrealized} />
                    <Stat label="Fees" value={-fees} negative />
                  </div>
                  <div className="rounded-md border border-border/40 bg-background/30 p-3">
                    <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                      Total P&L
                    </div>
                    <div
                      className={cn(
                        "num text-2xl font-semibold",
                        pnlClass(total),
                      )}
                    >
                      {formatUsd(total, { signed: true })}
                    </div>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </AppShell>
  );
}

function Stat({
  label,
  value,
  negative = false,
}: {
  label: string;
  value: number;
  negative?: boolean;
}) {
  return (
    <div className="rounded-md bg-background/40 p-2">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "num text-sm font-medium",
          negative ? "text-warn" : pnlClass(value),
        )}
      >
        {formatUsd(value, { signed: true })}
      </div>
    </div>
  );
}
