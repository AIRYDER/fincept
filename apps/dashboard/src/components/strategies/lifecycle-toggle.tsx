"use client";

/**
 * LifecycleToggle — start/stop control for a strategy config.
 *
 * Two visual modes:
 *
 *   - ``size="sm"`` (default): compact inline toggle for table rows.
 *     Shows a play/pause icon + state label; the whole chip is
 *     clickable.
 *   - ``size="lg"``: a full segmented control used on the detail
 *     page.  Two halves (Stop | Start) with the active half filled.
 *
 * Optimistic UI
 * ~~~~~~~~~~~~~
 *
 * React Query's optimistic updates flow:
 *   1. ``onMutate`` cancels any in-flight queries, snapshots the
 *      current cached list of configs, and writes the flipped
 *      ``enabled`` into the cache.
 *   2. The cached write makes the dot + label flip *before* the
 *      server round-trip lands -- operators get a responsive feel
 *      on a slow link.
 *   3. If the request errors, ``onError`` restores the snapshot so
 *      the UI reflects the real (still unchanged) server state.
 *   4. ``onSettled`` invalidates so the next refetch pulls truth.
 *
 * We intentionally do *not* rely on refetch latency to hide the
 * latency of the POST -- a dashboard that lags on every click feels
 * broken even if it's technically correct.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Pause, Play } from "lucide-react";
import { useState } from "react";

import { apiErrorMessage } from "@/components/strategies/use-api-error";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { StrategyConfigRow } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  config: StrategyConfigRow;
  size?: "sm" | "lg";
  className?: string;
  /** Called after a successful mutation.  Parent can close a dialog, etc. */
  onSuccess?: (next: StrategyConfigRow) => void;
}

export function LifecycleToggle({ config, size = "sm", className, onSuccess }: Props) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [lastError, setLastError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (nextEnabled: boolean) => {
      if (nextEnabled) return api.startStrategy(token, config.strategy_id);
      return api.stopStrategy(token, config.strategy_id);
    },
    onMutate: async (nextEnabled) => {
      setLastError(null);
      await queryClient.cancelQueries({ queryKey: ["strategies", "configs"] });
      const prevList = queryClient.getQueryData<StrategyConfigRow[]>([
        "strategies",
        "configs",
      ]);
      const prevOne = queryClient.getQueryData<StrategyConfigRow>([
        "strategies",
        "configs",
        config.strategy_id,
      ]);
      // Optimistic flip in both caches.
      if (prevList) {
        queryClient.setQueryData<StrategyConfigRow[]>(
          ["strategies", "configs"],
          prevList.map((c) =>
            c.strategy_id === config.strategy_id
              ? { ...c, enabled: nextEnabled }
              : c,
          ),
        );
      }
      if (prevOne) {
        queryClient.setQueryData<StrategyConfigRow>(
          ["strategies", "configs", config.strategy_id],
          { ...prevOne, enabled: nextEnabled },
        );
      }
      return { prevList, prevOne };
    },
    onError: (err, _next, ctx) => {
      // Roll back.
      if (ctx?.prevList) {
        queryClient.setQueryData(["strategies", "configs"], ctx.prevList);
      }
      if (ctx?.prevOne) {
        queryClient.setQueryData(
          ["strategies", "configs", config.strategy_id],
          ctx.prevOne,
        );
      }
      const msg = apiErrorMessage(err);
      setLastError(msg?.text ?? "failed");
    },
    onSuccess: (next) => {
      onSuccess?.(next);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies", "configs"] });
      queryClient.invalidateQueries({
        queryKey: ["strategies", "configs", config.strategy_id],
      });
    },
  });

  const enabled = config.enabled;
  const pending = mutation.isPending;

  if (size === "lg") {
    // Full segmented control for the detail page.
    return (
      <div className={cn("flex flex-col gap-1", className)}>
        <div
          className="inline-flex items-stretch overflow-hidden rounded-md border border-border"
          role="group"
          aria-label="Strategy lifecycle"
        >
          <button
            type="button"
            disabled={pending || !enabled}
            onClick={() => mutation.mutate(false)}
            className={cn(
              "inline-flex items-center gap-2 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest transition-colors",
              !enabled
                ? "bg-short/15 text-short"
                : "text-muted-foreground hover:bg-short/10 hover:text-short",
              pending && "opacity-60",
            )}
          >
            <Pause className="h-3.5 w-3.5" />
            Stop
          </button>
          <button
            type="button"
            disabled={pending || enabled}
            onClick={() => mutation.mutate(true)}
            className={cn(
              "inline-flex items-center gap-2 border-l border-border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest transition-colors",
              enabled
                ? "bg-long/15 text-long"
                : "text-muted-foreground hover:bg-long/10 hover:text-long",
              pending && "opacity-60",
            )}
          >
            {pending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            Start
          </button>
        </div>
        {lastError ? (
          <div className="flex items-center gap-1.5 text-[11px] text-destructive">
            <AlertTriangle className="h-3 w-3" />
            {lastError}
          </div>
        ) : null}
      </div>
    );
  }

  // Compact inline toggle (table rows).
  return (
    <button
      type="button"
      disabled={pending}
      onClick={(e) => {
        e.stopPropagation();
        mutation.mutate(!enabled);
      }}
      title={lastError ?? (enabled ? "Stop this strategy" : "Start this strategy")}
      className={cn(
        "group inline-flex items-center gap-1.5 border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest transition-all",
        enabled
          ? "border-long/40 bg-long/5 text-long hover:border-long/70 hover:bg-long/10"
          : "border-border/60 bg-background/30 text-muted-foreground hover:border-primary/50 hover:text-foreground",
        pending && "opacity-60",
        lastError && "border-destructive/50 text-destructive",
        className,
      )}
    >
      {pending ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : enabled ? (
        <Pause className="h-3 w-3 opacity-0 group-hover:opacity-100" />
      ) : (
        <Play className="h-3 w-3 opacity-0 group-hover:opacity-100" />
      )}
      <span className="min-w-[48px] text-center">
        {pending ? "…" : enabled ? "Running" : "Stopped"}
      </span>
    </button>
  );
}
