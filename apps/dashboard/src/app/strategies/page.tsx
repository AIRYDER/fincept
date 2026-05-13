"use client";

/**
 * /strategies — Phase F command center.
 *
 * Layout
 * ~~~~~~
 *
 *   [ KPI strip: total / running / stopped / orphans ]
 *   [ Toolbar: search · class filter · enabled filter · "new" CTA ]
 *   [ Unified table of strategy rows with inline lifecycle + actions ]
 *
 * Data model
 * ~~~~~~~~~~
 *
 * Three separate queries feed the view:
 *
 *   - ``strategyConfigs`` -- the persistent configs (Phase F source
 *     of truth).  This is the primary data.
 *   - ``strategies`` -- runtime view from the PositionStore.  Tells
 *     us "which strategy_ids have positions" so we can tag a config
 *     as *live* vs merely *enabled*, and tag position-only rows as
 *     *orphans* (no config -> manual orders or stale data).
 *   - ``positions`` -- all positions so we can compute realized +
 *     unrealized P&L per strategy_id without a separate round-trip
 *     per row.
 *
 * We merge all three into a single flat row list so the table can
 * filter/sort across "strategies with configs" and "strategies with
 * positions but no config" uniformly.
 */

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  Bot,
  Briefcase,
  Filter,
  Play,
  Search,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { AdoptStrategyDialog } from "@/components/strategies/adopt-strategy-dialog";
import { CreateStrategyDialog } from "@/components/strategies/create-strategy-dialog";
import { LifecycleToggle } from "@/components/strategies/lifecycle-toggle";
import { ModelBindingChip } from "@/components/strategies/model-binding-chip";
import { ParamsPreview } from "@/components/strategies/params-preview";
import { RowActions } from "@/components/strategies/row-actions";
import { StatusDot, type StrategyLiveState } from "@/components/strategies/status-dot";
import { SymbolsPills } from "@/components/strategies/symbols-pills";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  Position,
  StrategyConfigRow,
  StrategyRow,
} from "@/lib/types";
import { cn, formatUsd, pnlClass } from "@/lib/utils";

interface MergedRow {
  strategy_id: string;
  /** Null when this row is a runtime-only orphan. */
  config: StrategyConfigRow | null;
  /** Null when no positions exist under this strategy_id. */
  runtime: StrategyRow | null;
  positions: Position[];
  realized: number;
  unrealized: number;
  total: number;
  liveState: StrategyLiveState;
}

function asNum(v: string | null | undefined) {
  if (v == null) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export default function StrategiesPage() {
  const token = useAuth((s) => s.token);
  const [search, setSearch] = useState("");
  const [classFilter, setClassFilter] = useState<string | null>(null);
  const [enabledFilter, setEnabledFilter] = useState<
    "all" | "enabled" | "disabled"
  >("all");

  const configsQ = useQuery({
    queryKey: ["strategies", "configs"],
    queryFn: () => api.strategyConfigs(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
  const runtimeQ = useQuery({
    queryKey: ["strategies"],
    queryFn: () => api.strategies(token),
    enabled: !!token,
    refetchInterval: 30_000,
  });
  const positionsQ = useQuery({
    queryKey: ["positions", "all"],
    queryFn: () => api.positions(token, true),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  const rows: MergedRow[] = useMemo(() => {
    const configs = configsQ.data ?? [];
    const runtime = runtimeQ.data ?? [];
    const positions = positionsQ.data ?? [];

    const byId = new Map<string, MergedRow>();

    const positionsByStrategy = new Map<string, Position[]>();
    for (const p of positions) {
      if (!positionsByStrategy.has(p.strategy_id)) {
        positionsByStrategy.set(p.strategy_id, []);
      }
      positionsByStrategy.get(p.strategy_id)!.push(p);
    }

    const runtimeById = new Map(runtime.map((r) => [r.strategy_id, r]));

    const enrich = (
      strategy_id: string,
      config: StrategyConfigRow | null,
    ): MergedRow => {
      const pos = positionsByStrategy.get(strategy_id) ?? [];
      const runtimeRow = runtimeById.get(strategy_id) ?? null;
      const realized = pos.reduce((acc, p) => acc + asNum(p.realized_pnl), 0);
      const unrealized = pos.reduce(
        (acc, p) => acc + asNum(p.unrealized_pnl),
        0,
      );
      const total = realized + unrealized;
      const enabled = !!config?.enabled;
      const hasOpen = pos.some((p) => asNum(p.quantity) !== 0);
      const liveState: StrategyLiveState = enabled
        ? hasOpen
          ? "live"
          : "enabled"
        : "stopped";
      return {
        strategy_id,
        config,
        runtime: runtimeRow,
        positions: pos,
        realized,
        unrealized,
        total,
        liveState,
      };
    };

    for (const c of configs) {
      byId.set(c.strategy_id, enrich(c.strategy_id, c));
    }
    // Runtime rows without a config -> orphans, still worth surfacing.
    for (const r of runtime) {
      if (!byId.has(r.strategy_id)) {
        byId.set(r.strategy_id, enrich(r.strategy_id, null));
      }
    }

    return Array.from(byId.values()).sort((a, b) => {
      // Running strategies bubble to the top, then enabled, then the rest
      // alphabetical.
      const order = { live: 0, enabled: 1, stopped: 2 };
      if (order[a.liveState] !== order[b.liveState]) {
        return order[a.liveState] - order[b.liveState];
      }
      return a.strategy_id.localeCompare(b.strategy_id);
    });
  }, [configsQ.data, runtimeQ.data, positionsQ.data]);

  const filtered = useMemo(() => {
    return rows.filter((row) => {
      if (
        search &&
        !row.strategy_id.toLowerCase().includes(search.toLowerCase()) &&
        !row.config?.class_name.toLowerCase().includes(search.toLowerCase()) &&
        !row.config?.symbols.some((s) =>
          s.toLowerCase().includes(search.toLowerCase()),
        )
      ) {
        return false;
      }
      if (classFilter && row.config?.class_name !== classFilter) return false;
      if (enabledFilter === "enabled" && !row.config?.enabled) return false;
      if (enabledFilter === "disabled" && row.config?.enabled) return false;
      return true;
    });
  }, [rows, search, classFilter, enabledFilter]);

  // Summary stats, always against the unfiltered list -- the KPI strip
  // should reflect the whole portfolio, not the current filter view.
  const totalConfigs = configsQ.data?.length ?? 0;
  const running = rows.filter((r) => r.liveState === "live").length;
  const enabled = rows.filter((r) => r.liveState !== "stopped").length;
  const orphans = rows.filter((r) => r.config == null).length;
  const classOptions = useMemo(() => {
    const s = new Set<string>();
    for (const r of rows) if (r.config?.class_name) s.add(r.config.class_name);
    return Array.from(s).sort();
  }, [rows]);

  const clearFilters = () => {
    setSearch("");
    setClassFilter(null);
    setEnabledFilter("all");
  };
  const anyFilter = !!search || !!classFilter || enabledFilter !== "all";

  return (
    <AppShell>
      <PageHeader
        title="Strategies"
        description="Persistent strategy instances managed by the strategy-host service. Toggle any row to start or stop its runner on the next reconcile tick."
        action={<CreateStrategyDialog />}
      />

      {/* --- KPI strip ------------------------------------------------ */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi
          icon={Bot}
          label="Configs"
          value={totalConfigs}
          hint={`${orphans} runtime-only orphan${orphans === 1 ? "" : "s"}`}
        />
        <Kpi
          icon={Sparkles}
          label="Running"
          value={running}
          tone="long"
          pulse={running > 0}
          hint={`${enabled - running} enabled w/o positions`}
        />
        <Kpi
          icon={Briefcase}
          label="Open positions"
          value={rows.reduce(
            (acc, r) =>
              acc + r.positions.filter((p) => asNum(p.quantity) !== 0).length,
            0,
          )}
          hint={`${rows.reduce((a, r) => a + r.positions.length, 0)} total (incl. flat)`}
        />
        <Kpi
          icon={ShieldCheck}
          label="Net P&L"
          value={rows.reduce((acc, r) => acc + r.total, 0)}
          format="usd"
          tone="pnl"
        />
      </div>

      {/* --- Toolbar -------------------------------------------------- */}
      <div className="mt-6 flex flex-wrap items-center gap-2 border border-border/40 bg-background/30 p-2">
        <div className="relative min-w-[14rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search id · class · symbol"
            className="h-8 pl-8 font-mono text-xs"
          />
        </div>
        <div className="flex items-center gap-1">
          <Filter className="h-3 w-3 text-muted-foreground" />
          <FilterChip
            label="all"
            active={!classFilter}
            onClick={() => setClassFilter(null)}
          />
          {classOptions.map((c) => (
            <FilterChip
              key={c}
              label={c}
              active={classFilter === c}
              onClick={() =>
                setClassFilter(classFilter === c ? null : c)
              }
            />
          ))}
        </div>
        <div className="flex items-center gap-1">
          <FilterChip
            label="all"
            active={enabledFilter === "all"}
            onClick={() => setEnabledFilter("all")}
          />
          <FilterChip
            label="enabled"
            tone="long"
            active={enabledFilter === "enabled"}
            onClick={() => setEnabledFilter("enabled")}
          />
          <FilterChip
            label="stopped"
            tone="muted"
            active={enabledFilter === "disabled"}
            onClick={() => setEnabledFilter("disabled")}
          />
        </div>
        {anyFilter ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={clearFilters}
            className="ml-auto gap-1"
          >
            <X className="h-3 w-3" />
            Clear
          </Button>
        ) : (
          <span className="ml-auto text-[10px] uppercase tracking-widest text-muted-foreground">
            {filtered.length} of {rows.length}
          </span>
        )}
      </div>

      {/* --- Table --------------------------------------------------- */}
      <div className="mt-3 border border-border/60 bg-card">
        <div className="widget-header">
          <span>Strategy instances</span>
          <span>
            {configsQ.isLoading ? "loading…" : `${filtered.length} rows`}
          </span>
        </div>
        {rows.length === 0 && !configsQ.isLoading ? (
          <div className="p-3">
            <EmptyState
              icon={Bot}
              title="No strategies yet"
              description="Create a config with the “New strategy” button above.  The strategy-host supervisor will reconcile it into a running task within ~10s."
            />
          </div>
        ) : filtered.length === 0 ? (
          <div className="p-3">
            <EmptyState
              icon={Search}
              title="No matches"
              description="Adjust or clear the filters to see all rows."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-border/40 text-[10px] uppercase tracking-widest text-muted-foreground">
                  <th className="w-6 py-2 pl-3 pr-0"></th>
                  <th className="px-2 py-2">Strategy</th>
                  <th className="px-2 py-2">Class</th>
                  <th className="px-2 py-2">Symbols</th>
                  <th className="px-2 py-2">Binding</th>
                  <th className="px-2 py-2">Params</th>
                  <th className="px-2 py-2 text-right">Realized</th>
                  <th className="px-2 py-2 text-right">Unrealized</th>
                  <th className="px-2 py-2">Lifecycle</th>
                  <th className="w-[110px] px-2 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row, i) => (
                  <StrategyTableRow key={row.strategy_id} row={row} index={i} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </AppShell>
  );
}

// --------------------------------------------------------------------------- //
// Row                                                                         //
// --------------------------------------------------------------------------- //

function StrategyTableRow({
  row,
  index,
}: {
  row: MergedRow;
  index: number;
}) {
  const { config, liveState } = row;
  const orphan = config == null;
  const href = `/strategies/${encodeURIComponent(row.strategy_id)}`;
  const openSymbols = row.positions
    .filter((p) => asNum(p.quantity) !== 0)
    .map((p) => p.symbol)
    .sort();

  return (
    <motion.tr
      initial={{ opacity: 0, y: 3 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.015, 0.15) }}
      className={cn(
        "border-b border-border/30 transition-colors hover:bg-accent/30",
        orphan && "bg-warn/[0.03]",
      )}
    >
      <td className="w-6 py-2 pl-3 pr-0 align-middle">
        <StatusDot state={liveState} />
      </td>
      <td className="min-w-[10rem] px-2 py-2 align-middle">
        <Link
          href={href}
          className="group inline-flex items-center gap-1.5 font-mono text-[13px] font-semibold hover:text-primary"
        >
          <span className="truncate">{row.strategy_id}</span>
          {orphan ? (
            <span
              title="Has positions but no stored config — from a manual order or a since-deleted config"
              className="border border-warn/40 bg-warn/5 px-1 text-[9px] uppercase tracking-widest text-warn"
            >
              Orphan
            </span>
          ) : null}
        </Link>
      </td>
      <td className="px-2 py-2 align-middle">
        {config ? (
          <span className="inline-flex items-center border border-border/60 bg-background/40 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-foreground/80">
            {config.class_name}
          </span>
        ) : (
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            —
          </span>
        )}
      </td>
      <td className="px-2 py-2 align-middle">
        {config ? (
          <SymbolsPills symbols={config.symbols} />
        ) : row.runtime ? (
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {row.runtime.position_count} pos
          </span>
        ) : null}
      </td>
      <td className="px-2 py-2 align-middle">
        {config ? (
          <ModelBindingChip modelBinding={config.model_binding} compact />
        ) : null}
      </td>
      <td className="max-w-[14rem] px-2 py-2 align-middle">
        {config ? <ParamsPreview params={config.params} /> : null}
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-right align-middle">
        <span className={cn("num text-xs", pnlClass(row.realized))}>
          {formatUsd(row.realized, { signed: true })}
        </span>
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-right align-middle">
        <span className={cn("num text-xs", pnlClass(row.unrealized))}>
          {formatUsd(row.unrealized, { signed: true })}
        </span>
      </td>
      <td className="px-2 py-2 align-middle">
        {config ? (
          <LifecycleToggle config={config} />
        ) : (
          <span
            title="Orphan strategy — create a config to take ownership"
            className="inline-flex items-center gap-1 border border-dashed border-warn/40 bg-warn/5 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-warn"
          >
            <AlertTriangle className="h-3 w-3" />
            No config
          </span>
        )}
      </td>
      <td className="px-2 py-2 text-right align-middle">
        {config ? (
          <RowActions config={config} />
        ) : (
          <AdoptStrategyDialog
            strategyId={row.strategy_id}
            symbols={openSymbols}
          />
        )}
      </td>
    </motion.tr>
  );
}

// --------------------------------------------------------------------------- //
// KPI + filter chip primitives                                                //
// --------------------------------------------------------------------------- //

function Kpi({
  icon: Icon,
  label,
  value,
  hint,
  tone = "default",
  format = "int",
  pulse = false,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number;
  hint?: string;
  tone?: "default" | "long" | "pnl";
  format?: "int" | "usd";
  pulse?: boolean;
}) {
  const formatted =
    format === "usd"
      ? formatUsd(value, { signed: true, compact: true })
      : value.toLocaleString("en-US");

  const valueClass =
    tone === "pnl"
      ? pnlClass(value)
      : tone === "long"
        ? "text-long"
        : "text-foreground";

  return (
    <div
      className={cn(
        "relative overflow-hidden border border-border/60 bg-background/30 p-3",
        tone === "long" && "border-long/20 bg-long/[0.03]",
      )}
    >
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
        {pulse ? (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-long">
            <span className="live-dot" />
            Live
          </span>
        ) : null}
      </div>
      <div className={cn("mt-1 font-mono text-2xl font-bold", valueClass)}>
        {formatted}
      </div>
      {hint ? (
        <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>
      ) : null}
    </div>
  );
}

function FilterChip({
  label,
  active,
  onClick,
  tone = "default",
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  tone?: "default" | "long" | "muted";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex h-6 items-center border px-2 text-[10px] font-semibold uppercase tracking-widest transition-colors",
        active
          ? tone === "long"
            ? "border-long/60 bg-long/10 text-long"
            : "border-primary/60 bg-primary/10 text-primary"
          : "border-border/60 bg-background/30 text-muted-foreground hover:border-primary/40 hover:text-foreground",
      )}
    >
      {active && tone === "default" ? (
        <Play className="mr-1 h-2.5 w-2.5" />
      ) : null}
      {label}
    </button>
  );
}
