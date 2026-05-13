"use client";

/**
 * /strategies/[id] — strategy instance detail.
 *
 * Three-column layout on wide screens, single-column stacked on
 * narrow:
 *
 *   LEFT / main
 *     - Identity header (status dot + id + class + symbols).
 *     - Lifecycle segmented control (Stop | Start).
 *     - Positions table scoped to this strategy (Redis-backed).
 *     - Audit timeline (StrategyHistoryPanel).
 *
 *   RIGHT / sidebar
 *     - Model binding card with live active-model state.
 *     - Params card.
 *     - Danger zone with type-to-confirm delete.
 *
 * The positions table reuses the same compact columns the
 * /positions page uses; the design goal is for an operator who
 * knows the positions layout to recognise it instantly here.
 */

import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import {
  AlertTriangle,
  ArrowLeft,
  Briefcase,
  Clock,
  History,
  Layers,
  Link2,
  Pencil,
  SlidersHorizontal,
  Trash2,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { DeleteStrategyDialog } from "@/components/strategies/delete-strategy-dialog";
import { EditStrategyDialog } from "@/components/strategies/edit-strategy-dialog";
import { StrategyHistoryPanel } from "@/components/strategies/history-panel";
import { LifecycleToggle } from "@/components/strategies/lifecycle-toggle";
import { ModelBindingChip } from "@/components/strategies/model-binding-chip";
import { ParamsPreview } from "@/components/strategies/params-preview";
import { StrategyReadinessPanel } from "@/components/strategies/strategy-readiness-panel";
import {
  StatusDot,
  type StrategyLiveState,
} from "@/components/strategies/status-dot";
import { SymbolsPills } from "@/components/strategies/symbols-pills";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/widgets/empty-state";
import { PageHeader } from "@/components/widgets/page-header";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Position, StrategyConfigRow } from "@/lib/types";
import { cn, formatUsd, pnlClass } from "@/lib/utils";

export default function StrategyDetailPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params?.id ?? "");
  const token = useAuth((s) => s.token);

  const [editOpen, setEditOpen] = useState(false);

  const detail = useQuery({
    queryKey: ["strategies", "configs", id],
    queryFn: () => api.strategyConfig(token, id),
    enabled: !!token && !!id,
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: (count, err) =>
      err instanceof ApiError && err.status === 404 ? false : count < 2,
  });

  const positions = useQuery({
    queryKey: ["positions", "strategy", id],
    queryFn: () => api.strategyPositions(token, id, true),
    enabled: !!token && !!id,
    refetchInterval: 30_000,
  });

  const config = detail.data;
  const has404 =
    detail.error instanceof ApiError && detail.error.status === 404;

  if (has404) {
    return (
      <AppShell>
        <PageHeader
          title={
            <span className="flex items-center gap-3 font-mono">
              <Link
                href="/strategies"
                className="text-muted-foreground hover:text-foreground"
                aria-label="Back to strategies"
              >
                <ArrowLeft className="h-5 w-5" />
              </Link>
              {id}
            </span>
          }
          description="No such strategy config."
        />
        <EmptyState
          icon={AlertTriangle}
          title="Strategy not found"
          description="It may have been deleted, or the id was mistyped."
        />
      </AppShell>
    );
  }

  const liveState: StrategyLiveState = (() => {
    if (!config?.enabled) return "stopped";
    const hasOpen = (positions.data ?? []).some(
      (p) => Number(p.quantity) !== 0,
    );
    return hasOpen ? "live" : "enabled";
  })();

  return (
    <AppShell>
      <PageHeader
        title={
          <span className="flex items-center gap-3 font-mono">
            <Link
              href="/strategies"
              className="text-muted-foreground hover:text-foreground"
              aria-label="Back to strategies"
            >
              <ArrowLeft className="h-5 w-5" />
            </Link>
            <StatusDot state={liveState} />
            {id}
          </span>
        }
        description={
          config
            ? `${config.class_name} · ${config.symbols.length} symbol${config.symbols.length === 1 ? "" : "s"}${config.model_binding ? ` · bound to ${config.model_binding}` : ""}`
            : "Loading…"
        }
        action={
          config ? (
            <div className="flex items-center gap-3">
              <LifecycleToggle size="lg" config={config} />
              <Button
                size="sm"
                variant="outline"
                onClick={() => setEditOpen(true)}
                className="gap-2"
              >
                <Pencil className="h-3.5 w-3.5" />
                Edit
              </Button>
              <EditStrategyDialog
                config={config}
                open={editOpen}
                onOpenChange={setEditOpen}
              />
            </div>
          ) : null
        }
      />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        {/* Main column ------------------------------------------------ */}
        <div className="space-y-4 xl:col-span-2">
          <SymbolsCard config={config} />
          <PositionsCard
            positions={positions.data ?? []}
            isLoading={positions.isLoading}
          />
          <HistoryCard id={id} />
        </div>

        {/* Sidebar ---------------------------------------------------- */}
        <div className="space-y-4">
          {config ? (
            <StrategyReadinessPanel
              config={config}
              positions={positions.data ?? []}
            />
          ) : null}
          <BindingCard
            binding={config?.model_binding ?? null}
            isLoading={detail.isLoading}
          />
          <ParamsCard params={config?.params ?? null} />
          <MetaCard
            createdAt={config?.created_at ?? null}
            updatedAt={config?.updated_at ?? null}
          />
          {config ? (
            <DangerZoneCard id={id} onEdit={() => setEditOpen(true)} configRef={config} />
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}

// --------------------------------------------------------------------------- //
// Cards                                                                       //
// --------------------------------------------------------------------------- //

function SymbolsCard({
  config,
}: {
  config: StrategyConfigRow | undefined;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Layers className="mr-1 h-3.5 w-3.5" />
          Symbols
        </CardTitle>
        <CardDescription>
          The basket this strategy trades.  The host fans bars for each
          of these into the strategy on_bar hook.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {config ? (
          config.symbols.length === 0 ? (
            <span className="text-xs text-muted-foreground">
              No symbols — strategy will never receive on_bar.
            </span>
          ) : (
            <SymbolsPills symbols={config.symbols} max={64} />
          )
        ) : (
          <Skeleton lines={1} />
        )}
      </CardContent>
    </Card>
  );
}

function PositionsCard({
  positions,
  isLoading,
}: {
  positions: Position[];
  isLoading: boolean;
}) {
  const open = positions.filter((p) => Number(p.quantity) !== 0);
  const realized = positions.reduce(
    (a, p) => a + (Number(p.realized_pnl) || 0),
    0,
  );
  const unrealized = positions.reduce(
    (a, p) => a + (Number(p.unrealized_pnl) || 0),
    0,
  );
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>
          <Briefcase className="mr-1 h-3.5 w-3.5" />
          Positions
        </CardTitle>
        <CardDescription>
          {open.length} open / {positions.length} tracked ·{" "}
          <span className={pnlClass(realized)}>
            realized {formatUsd(realized, { signed: true })}
          </span>{" "}
          ·{" "}
          <span className={pnlClass(unrealized)}>
            unrealized {formatUsd(unrealized, { signed: true })}
          </span>
        </CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="p-3">
            <Skeleton lines={3} />
          </div>
        ) : positions.length === 0 ? (
          <div className="p-3">
            <EmptyState
              icon={Briefcase}
              title="No positions yet"
              description="The strategy hasn't traded yet.  Positions appear here as soon as the first fill lands."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-border/40 text-[10px] uppercase tracking-widest text-muted-foreground">
                  <th className="px-3 py-2">Symbol</th>
                  <th className="px-3 py-2 text-right">Qty</th>
                  <th className="px-3 py-2 text-right">Avg cost</th>
                  <th className="px-3 py-2 text-right">Mark</th>
                  <th className="px-3 py-2 text-right">Unrealized</th>
                  <th className="px-3 py-2 text-right">Realized</th>
                  <th className="px-3 py-2 text-right">Updated</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => {
                  const qty = Number(p.quantity);
                  const u = Number(p.unrealized_pnl);
                  const r = Number(p.realized_pnl);
                  return (
                    <tr
                      key={`${p.symbol}-${p.strategy_id}`}
                      className="border-b border-border/30"
                    >
                      <td className="px-3 py-1.5 font-mono text-xs font-semibold">
                        {p.symbol}
                      </td>
                      <td
                        className={cn(
                          "whitespace-nowrap px-3 py-1.5 text-right font-mono text-xs",
                          qty > 0 && "text-long",
                          qty < 0 && "text-short",
                          qty === 0 && "text-muted-foreground",
                        )}
                      >
                        {qty === 0 ? "flat" : p.quantity}
                      </td>
                      <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono text-xs">
                        {formatUsd(p.avg_cost)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono text-xs text-muted-foreground">
                        {p.mark_px ? formatUsd(p.mark_px) : "—"}
                      </td>
                      <td
                        className={cn(
                          "whitespace-nowrap px-3 py-1.5 text-right font-mono text-xs",
                          pnlClass(u),
                        )}
                      >
                        {formatUsd(u, { signed: true })}
                      </td>
                      <td
                        className={cn(
                          "whitespace-nowrap px-3 py-1.5 text-right font-mono text-xs",
                          pnlClass(r),
                        )}
                      >
                        {formatUsd(r, { signed: true })}
                      </td>
                      <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono text-[10px] text-muted-foreground">
                        {p.updated_at
                          ? formatDistanceToNow(new Date(p.updated_at * 1000), {
                              addSuffix: true,
                            })
                          : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function HistoryCard({ id }: { id: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <History className="mr-1 h-3.5 w-3.5" />
          History
        </CardTitle>
        <CardDescription>
          Full audit trail from the JSONL store.  Newest first; changed
          fields are highlighted against the previous snapshot.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <StrategyHistoryPanel strategyId={id} />
      </CardContent>
    </Card>
  );
}

function BindingCard({
  binding,
  isLoading,
}: {
  binding: string | null;
  isLoading: boolean;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Link2 className="mr-1 h-3.5 w-3.5" />
          Model binding
        </CardTitle>
        <CardDescription>
          Which agent active model this strategy reloads when you
          promote.  Only meaningful for ML-backed strategies.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton lines={1} />
        ) : (
          <div className="flex flex-col gap-2">
            <ModelBindingChip modelBinding={binding} />
            {binding ? (
              <Link
                href="/models"
                className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary"
              >
                Manage model promotions →
              </Link>
            ) : (
              <p className="text-[10px] text-muted-foreground">
                No binding set.  Patch this strategy with a{" "}
                <code className="font-mono">model_binding</code> to
                hot-reload on promote.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ParamsCard({ params }: { params: Record<string, unknown> | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <SlidersHorizontal className="mr-1 h-3.5 w-3.5" />
          Params
        </CardTitle>
        <CardDescription>
          Constructor kwargs passed to the strategy class at
          instantiation.  Changes apply on the next runner restart.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {params == null ? (
          <Skeleton lines={2} />
        ) : Object.keys(params).length === 0 ? (
          <span className="inline-flex items-center border border-dashed border-border/60 bg-background/30 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            default params
          </span>
        ) : (
          <>
            <dl className="grid grid-cols-[minmax(0,auto)_1fr] gap-x-4 gap-y-1 font-mono text-[11px]">
              {Object.entries(params).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="uppercase tracking-widest text-muted-foreground">
                    {k}
                  </dt>
                  <dd className="break-all text-foreground/90">
                    {stringify(v)}
                  </dd>
                </div>
              ))}
            </dl>
            <details className="mt-3">
              <summary className="cursor-pointer text-[10px] uppercase tracking-widest text-muted-foreground transition-colors hover:text-foreground">
                Raw JSON
              </summary>
              <pre className="mt-2 max-h-40 overflow-auto rounded bg-background/60 p-2 text-[10px] leading-relaxed text-foreground/80">
                {JSON.stringify(params, null, 2)}
              </pre>
            </details>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function MetaCard({
  createdAt,
  updatedAt,
}: {
  createdAt: number | null;
  updatedAt: number | null;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Clock className="mr-1 h-3.5 w-3.5" />
          Meta
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-[11px]">
        <Row
          label="Created"
          value={
            createdAt
              ? `${formatDistanceToNow(new Date(createdAt * 1000), {
                  addSuffix: true,
                })} (${new Date(createdAt * 1000).toLocaleString()})`
              : "—"
          }
        />
        <Row
          label="Updated"
          value={
            updatedAt
              ? `${formatDistanceToNow(new Date(updatedAt * 1000), {
                  addSuffix: true,
                })} (${new Date(updatedAt * 1000).toLocaleString()})`
              : "—"
          }
        />
      </CardContent>
    </Card>
  );
}

function DangerZoneCard({
  id,
  onEdit,
  configRef,
}: {
  id: string;
  onEdit: () => void;
  configRef: StrategyConfigRow;
}) {
  return (
    <Card className="border-destructive/30 bg-destructive/[0.02]">
      <CardHeader>
        <CardTitle className="text-destructive">
          <AlertTriangle className="mr-1 h-3.5 w-3.5" />
          Danger zone
        </CardTitle>
        <CardDescription>
          Destructive actions.  The audit timeline is retained so the
          history above stays inspectable even after deletion.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={onEdit}
          className="w-full justify-start gap-2"
        >
          <Workflow className="h-3.5 w-3.5" />
          Change class / rebind model
        </Button>
        <DeleteStrategyDialog
          config={configRef}
          redirectOnSuccess
          trigger={
            <Button
              variant="outline"
              size="sm"
              className="w-full justify-start gap-2 border-destructive/40 text-destructive hover:border-destructive hover:bg-destructive/10"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete {id}
            </Button>
          }
        />
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Helpers                                                                     //
// --------------------------------------------------------------------------- //

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="shrink-0 uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <span className="truncate text-right font-mono text-foreground/90">
        {value}
      </span>
    </div>
  );
}

function Skeleton({ lines = 1 }: { lines?: number }) {
  return (
    <div className="space-y-1.5">
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="h-3 animate-pulse bg-muted/40"
          style={{ width: `${60 + ((i * 13) % 35)}%` }}
        />
      ))}
    </div>
  );
}

function stringify(v: unknown): string {
  if (v === null) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
