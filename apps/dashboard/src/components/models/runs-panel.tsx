"use client";

/**
 * RunsPanel -- compact runs-history table with on-demand log tail.
 *
 * Polling is adaptive:
 *
 *   * 3s while *any* run is queued/running.  Operator has just
 *     submitted; tight loop keeps the status feeling live.
 *   * 30s otherwise.  History is mostly static so we don't burn
 *     pointless cycles.
 *
 * Selecting a row fetches the run's log tail (a separate query so the
 * listing payload stays small).  We fetch the log lazily because most
 * sessions only ever inspect one or two failed runs in detail.
 */

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Clock,
  Loader2,
  ScrollText,
  XCircle,
} from "lucide-react";
import { useState } from "react";

import { EmptyState } from "@/components/widgets/empty-state";
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
import type { TrainingRun, TrainingRunStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

export function RunsPanel() {
  const token = useAuth((s) => s.token);
  const [selected, setSelected] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["models", "runs"],
    queryFn: () => api.modelRuns(token, { limit: 50 }),
    enabled: !!token,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (
        data &&
        (data.summary.running > 0 || data.summary.queued > 0)
      ) {
        return 3_000;
      }
      return 30_000;
    },
    staleTime: 1_000,
  });

  // Auto-select the most recent run on first load so the panel isn't
  // empty for the operator.
  const runs = list.data?.runs ?? [];
  if (selected === null && runs.length > 0) {
    setSelected(runs[0].run_id);
  }

  return (
    <Card className="mt-6">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          <ScrollText className="h-4 w-4 text-primary" />
          Training runs
        </CardTitle>
        <CardDescription>
          Last 50 runs.  Click any row to see its tail of stdout +
          stderr from the trainer subprocess.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {list.error ? (
          <EmptyState
            icon={AlertTriangle}
            title="Failed to load runs"
            description={
              list.error instanceof Error
                ? list.error.message
                : "Unknown error"
            }
          />
        ) : list.isLoading ? (
          <EmptyState
            icon={Loader2}
            title="Loading runs…"
            description="Polling /models/runs."
          />
        ) : runs.length === 0 ? (
          <EmptyState
            icon={ScrollText}
            title="No runs yet"
            description="Click 'Train new model' above to launch one."
          />
        ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_3fr]">
            <RunsList
              runs={runs}
              selected={selected}
              onSelect={(id) => setSelected(id)}
            />
            {selected ? <RunDetail runId={selected} /> : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Listing                                                                    //
// --------------------------------------------------------------------------- //

function RunsList({
  runs,
  selected,
  onSelect,
}: {
  runs: TrainingRun[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="max-h-[28rem] divide-y divide-border/30 overflow-y-auto rounded-md border border-border/40 bg-background/30 scrollbar-thin">
      {runs.map((r) => {
        const isSelected = r.run_id === selected;
        return (
          <button
            key={r.run_id}
            type="button"
            onClick={() => onSelect(r.run_id)}
            className={cn(
              "group flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition-colors",
              isSelected
                ? "bg-primary/10 text-foreground"
                : "hover:bg-accent/40",
            )}
          >
            <StatusDot status={r.status} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate font-mono text-xs font-semibold">
                  {r.request.model_name}
                </span>
                <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  {formatRelativeAge(r.created_at)}
                </span>
              </div>
              <div className="flex items-baseline gap-2 text-[11px] text-muted-foreground">
                <span>cv={r.request.cv_folds}</span>
                <span>·</span>
                <span>h={r.request.horizon_bars}b</span>
                {r.duration_seconds != null ? (
                  <>
                    <span>·</span>
                    <span>{formatDuration(r.duration_seconds)}</span>
                  </>
                ) : null}
              </div>
            </div>
            <ChevronRight
              className={cn(
                "h-3.5 w-3.5 shrink-0 transition-transform",
                isSelected ? "translate-x-0.5 text-primary" : "text-muted-foreground",
              )}
            />
          </button>
        );
      })}
    </div>
  );
}

function StatusDot({ status }: { status: TrainingRunStatus }) {
  const conf = {
    queued: { Icon: Clock, color: "text-muted-foreground" },
    running: { Icon: Loader2, color: "text-primary animate-spin" },
    completed: { Icon: CheckCircle2, color: "text-long" },
    failed: { Icon: XCircle, color: "text-destructive" },
  }[status];
  const Icon = conf.Icon;
  return <Icon className={cn("h-3.5 w-3.5 shrink-0", conf.color)} />;
}

// --------------------------------------------------------------------------- //
// Detail                                                                     //
// --------------------------------------------------------------------------- //

function RunDetail({ runId }: { runId: string }) {
  const token = useAuth((s) => s.token);
  const detail = useQuery({
    queryKey: ["models", "runs", runId],
    queryFn: () => api.modelRunDetail(token, runId),
    enabled: !!token && !!runId,
    refetchInterval: (query) => {
      // Same adaptive cadence as the listing -- but for a single run.
      const r = query.state.data;
      if (r && (r.status === "running" || r.status === "queued")) {
        return 3_000;
      }
      return 30_000;
    },
    staleTime: 1_000,
  });

  if (detail.isLoading || !detail.data) {
    return (
      <div className="flex items-center justify-center rounded-md border border-border/40 bg-background/30 p-6 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading run…
      </div>
    );
  }
  const r = detail.data;
  return (
    <div className="space-y-3 rounded-md border border-border/40 bg-background/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold">
          {r.request.model_name}
        </span>
        <StatusBadge status={r.status} />
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] uppercase tracking-widest text-muted-foreground sm:grid-cols-3">
        <span>created · {formatRelativeAge(r.created_at)}</span>
        <span>
          duration · {r.duration_seconds != null ? formatDuration(r.duration_seconds) : "—"}
        </span>
        <span>
          exit · {r.exit_code != null ? r.exit_code : r.pid != null ? `pid ${r.pid}` : "—"}
        </span>
        <span>cv folds · {r.request.cv_folds}</span>
        <span>horizon · {r.request.horizon_bars}b</span>
        <span>boost rounds · {r.request.num_boost_round}</span>
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Input
        </div>
        <code className="block break-all rounded bg-background/40 p-1.5 font-mono text-[11px]">
          {r.request.input_path}
        </code>
      </div>

      {r.error ? (
        <div className="rounded border border-destructive/40 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">
          {r.error}
        </div>
      ) : null}

      <div>
        <div className="mb-1 text-[10px] uppercase tracking-widest text-muted-foreground">
          Log tail
        </div>
        <pre className="max-h-72 overflow-y-auto rounded bg-black/40 p-2 font-mono text-[11px] leading-snug text-zinc-300 scrollbar-thin">
          {(r.log_tail ?? []).length === 0
            ? "(no output yet)"
            : (r.log_tail ?? []).join("\n")}
        </pre>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: TrainingRunStatus }) {
  const variant = {
    queued: "muted" as const,
    running: "default" as const,
    completed: "long" as const,
    failed: "destructive" as const,
  }[status];
  return (
    <Badge variant={variant} className="font-mono uppercase tracking-widest">
      {status}
    </Badge>
  );
}

// --------------------------------------------------------------------------- //
// Format helpers                                                             //
// --------------------------------------------------------------------------- //

function formatRelativeAge(unixSeconds: number): string {
  const delta = Date.now() / 1000 - unixSeconds;
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}

function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
