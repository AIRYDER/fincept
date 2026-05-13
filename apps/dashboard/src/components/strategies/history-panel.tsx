"use client";

/**
 * StrategyHistoryPanel — audit timeline for a strategy config.
 *
 * The JSONL audit store keeps a full snapshot of the config on
 * every write (create, every PATCH, every toggle).  Showing a
 * timeline of snapshots is useful but visually noisy -- most
 * entries are tiny diffs against the previous entry.  We help the
 * operator scan by:
 *
 *   1. Diffing each snapshot against its successor (older) and
 *      rendering only the *changed* fields as highlighted chips.
 *   2. Falling back to a compact summary ("restarted", "params
 *      changed") when multiple fields changed at once.
 *   3. Rendering the lifecycle events (enabled flips) with the same
 *      colour language as the LifecycleToggle so the timeline reads
 *      like a continuation of the live control.
 *
 * The newest entry is at the top and doesn't have a "vs previous"
 * row; it's the current state, rendered full.
 */

import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { motion } from "framer-motion";
import {
  ArrowRight,
  Boxes,
  Clock,
  History,
  Pause,
  Pencil,
  Play,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useMemo } from "react";

import { EmptyState } from "@/components/widgets/empty-state";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { StrategyConfigRow } from "@/lib/types";
import { cn } from "@/lib/utils";

type ChangeKind =
  | "create"
  | "delete"
  | "start"
  | "stop"
  | "class_change"
  | "symbols"
  | "params"
  | "model_binding"
  | "mixed"
  | "no_change";

interface DiffSummary {
  kind: ChangeKind;
  fields: string[];
  label: string;
}

export function StrategyHistoryPanel({ strategyId }: { strategyId: string }) {
  const token = useAuth((s) => s.token);

  const { data, isLoading } = useQuery({
    queryKey: ["strategies", "history", strategyId],
    queryFn: () => api.strategyHistory(token, strategyId, 100),
    enabled: !!token && !!strategyId,
    staleTime: 10_000,
  });

  const entries = useMemo(() => data ?? [], [data]);

  // Each entry is diffed against its SUCCESSOR in the list (index+1),
  // which is the older entry -- the api returns newest-first.
  const diffs = useMemo(() => {
    return entries.map((cur, i) => diffAgainst(cur, entries[i + 1]));
  }, [entries]);

  if (isLoading) {
    return (
      <EmptyState
        icon={History}
        title="Loading history…"
        description="Reading the audit JSONL."
      />
    );
  }

  if (entries.length === 0) {
    return (
      <EmptyState
        icon={History}
        title="No history recorded"
        description="Every write to this strategy (create, edit, toggle) will appear here."
      />
    );
  }

  return (
    <div className="relative pl-5">
      {/* vertical connector line — sibling of <ol> so we don't violate
          the "ol may only contain li" rule. */}
      <span className="pointer-events-none absolute bottom-2 left-[9px] top-2 w-px bg-gradient-to-b from-border/60 via-border/30 to-transparent" />
      <ol className="space-y-3">
      {entries.map((entry, i) => {
        const diff = diffs[i];
        const isCurrent = i === 0;
        return (
          <motion.li
            key={`${entry.updated_at}-${i}`}
            initial={{ opacity: 0, x: -4 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: Math.min(i * 0.02, 0.2) }}
            className="relative"
          >
            {/* Timeline dot */}
            <span
              className={cn(
                "absolute -left-5 top-1.5 flex h-4 w-4 items-center justify-center rounded-full border bg-card",
                kindTone(diff.kind).dotClass,
              )}
            >
              <KindIcon kind={diff.kind} />
            </span>

            <div
              className={cn(
                "border p-3 transition-colors",
                isCurrent
                  ? "border-primary/40 bg-primary/5"
                  : "border-border/60 bg-background/30",
              )}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "text-[11px] font-semibold uppercase tracking-widest",
                      kindTone(diff.kind).textClass,
                    )}
                  >
                    {diff.label}
                  </span>
                  {isCurrent ? (
                    <span className="border border-primary/30 bg-primary/10 px-1 font-mono text-[9px] uppercase tracking-widest text-primary">
                      Current
                    </span>
                  ) : null}
                </div>
                <time
                  dateTime={new Date(entry.updated_at * 1000).toISOString()}
                  title={new Date(entry.updated_at * 1000).toLocaleString()}
                  className="inline-flex items-center gap-1 text-[10px] text-muted-foreground"
                >
                  <Clock className="h-3 w-3" />
                  {formatDistanceToNow(new Date(entry.updated_at * 1000), {
                    addSuffix: true,
                  })}
                </time>
              </div>

              {diff.fields.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {diff.fields.map((field) => (
                    <DiffChip
                      key={field}
                      field={field}
                      cur={entry}
                      prev={entries[i + 1]}
                    />
                  ))}
                </div>
              ) : null}

              {/* Full snapshot summary -- a small reminder of the config
                  at that point in time, useful for the creation entry. */}
              {diff.kind === "create" || isCurrent ? (
                <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px] font-mono text-muted-foreground">
                  <span>class:</span>
                  <span className="text-foreground/80">{entry.class_name}</span>
                  <span>symbols:</span>
                  <span className="text-foreground/80">
                    {entry.symbols.length > 0
                      ? entry.symbols.join(", ")
                      : "—"}
                  </span>
                  {entry.model_binding ? (
                    <>
                      <span>model_binding:</span>
                      <span className="text-foreground/80">
                        {entry.model_binding}
                      </span>
                    </>
                  ) : null}
                  <span>enabled:</span>
                  <span
                    className={cn(
                      "font-semibold",
                      entry.enabled ? "text-long" : "text-muted-foreground",
                    )}
                  >
                    {entry.enabled ? "true" : "false"}
                  </span>
                </div>
              ) : null}
            </div>
          </motion.li>
        );
      })}
      </ol>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Diff helpers                                                                //
// --------------------------------------------------------------------------- //

function diffAgainst(
  cur: StrategyConfigRow,
  prev: StrategyConfigRow | undefined,
): DiffSummary {
  if (!prev) {
    // Oldest record -- this is the create.
    return { kind: "create", fields: [], label: "Created" };
  }
  if (cur.class_name === "(deleted)") {
    return { kind: "delete", fields: [], label: "Deleted" };
  }

  const fields: string[] = [];
  if (cur.class_name !== prev.class_name) fields.push("class_name");
  if (!sameList(cur.symbols, prev.symbols)) fields.push("symbols");
  if (JSON.stringify(cur.params) !== JSON.stringify(prev.params)) {
    fields.push("params");
  }
  if ((cur.model_binding ?? null) !== (prev.model_binding ?? null)) {
    fields.push("model_binding");
  }
  const enabledChanged = cur.enabled !== prev.enabled;
  if (enabledChanged) fields.push("enabled");

  if (fields.length === 0) return { kind: "no_change", fields, label: "Touched" };
  if (fields.length === 1 && enabledChanged) {
    return {
      kind: cur.enabled ? "start" : "stop",
      fields: ["enabled"],
      label: cur.enabled ? "Started" : "Stopped",
    };
  }
  if (fields.length === 1) {
    const only = fields[0];
    switch (only) {
      case "class_name":
        return { kind: "class_change", fields, label: "Class changed" };
      case "symbols":
        return { kind: "symbols", fields, label: "Symbols updated" };
      case "params":
        return { kind: "params", fields, label: "Params updated" };
      case "model_binding":
        return { kind: "model_binding", fields, label: "Binding updated" };
    }
  }
  return { kind: "mixed", fields, label: `${fields.length} fields changed` };
}

function sameList(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function kindTone(kind: ChangeKind): {
  dotClass: string;
  textClass: string;
} {
  switch (kind) {
    case "create":
      return {
        dotClass: "border-primary/40 text-primary",
        textClass: "text-primary",
      };
    case "start":
      return { dotClass: "border-long/40 text-long", textClass: "text-long" };
    case "stop":
      return {
        dotClass: "border-short/40 text-short",
        textClass: "text-short",
      };
    case "delete":
      return {
        dotClass: "border-destructive/40 text-destructive",
        textClass: "text-destructive",
      };
    default:
      return {
        dotClass: "border-border text-cyan",
        textClass: "text-cyan",
      };
  }
}

function KindIcon({ kind }: { kind: ChangeKind }) {
  const cls = "h-2.5 w-2.5";
  switch (kind) {
    case "create":
      return <Sparkles className={cls} />;
    case "start":
      return <Play className={cls} />;
    case "stop":
      return <Pause className={cls} />;
    case "delete":
      return <Trash2 className={cls} />;
    case "symbols":
      return <Boxes className={cls} />;
    default:
      return <Pencil className={cls} />;
  }
}

function DiffChip({
  field,
  cur,
  prev,
}: {
  field: string;
  cur: StrategyConfigRow;
  prev: StrategyConfigRow | undefined;
}) {
  const oldValue = formatField(field, prev);
  const newValue = formatField(field, cur);
  return (
    <span
      title={`${field}: ${oldValue ?? "—"} → ${newValue ?? "—"}`}
      className="inline-flex max-w-full items-center gap-1 border border-border/60 bg-background/40 px-1.5 py-0.5 font-mono text-[10px]"
    >
      <span className="uppercase tracking-widest text-muted-foreground">
        {field}
      </span>
      {oldValue != null && oldValue !== "—" ? (
        <span className="line-through decoration-short/70 decoration-[1px] text-muted-foreground/70">
          {truncate(oldValue, 16)}
        </span>
      ) : null}
      <ArrowRight className="h-2.5 w-2.5 text-muted-foreground/50" />
      <span className="truncate text-foreground/90">
        {truncate(newValue ?? "—", 24)}
      </span>
    </span>
  );
}

function formatField(
  field: string,
  c: StrategyConfigRow | undefined,
): string | null {
  if (!c) return null;
  switch (field) {
    case "class_name":
      return c.class_name;
    case "symbols":
      return c.symbols.length === 0 ? "—" : c.symbols.join(", ");
    case "params":
      return Object.keys(c.params).length === 0
        ? "default"
        : JSON.stringify(c.params);
    case "model_binding":
      return c.model_binding ?? "—";
    case "enabled":
      return c.enabled ? "true" : "false";
    default:
      return null;
  }
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1) + "…";
}
