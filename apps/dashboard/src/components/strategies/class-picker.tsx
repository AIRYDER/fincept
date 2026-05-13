"use client";

/**
 * ClassPicker — segmented selector of a class_name from the live
 * STRATEGY_REGISTRY served by ``GET /backtest/strategies``.
 *
 * Why fetch from the server
 * ~~~~~~~~~~~~~~~~~~~~~~~~~
 *
 * The registry lives in ``backtester.strategies.STRATEGY_REGISTRY``;
 * hard-coding the three current keys in the UI would silently drift
 * the next time someone adds a strategy class.  The api already
 * exposes the registry for the backtest page, so we reuse the same
 * endpoint here for the single source of truth.
 *
 * While the list is loading we show the three well-known keys as
 * disabled shimmers to avoid a layout-shift pop-in when the request
 * resolves.
 */

import { useQuery } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const KNOWN_KEYS = [
  "buy_and_hold",
  "position_tracker",
  "ma_crossover",
  "gbm",
] as const;

export function ClassPicker({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  const token = useAuth((s) => s.token);
  const { data, isLoading } = useQuery({
    queryKey: ["backtest", "strategies"],
    queryFn: () => api.backtestStrategies(token),
    enabled: !!token,
    staleTime: 5 * 60_000,
  });

  const keys = data?.strategies?.map((s) => s.key) ?? KNOWN_KEYS;
  const descByKey = new Map(
    (data?.strategies ?? []).map((s) => [s.key, s.description]),
  );

  return (
    <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-4">
      {keys.map((key) => {
        const selected = value === key;
        const desc = descByKey.get(key);
        return (
          <button
            key={key}
            type="button"
            disabled={disabled || isLoading}
            onClick={() => onChange(key)}
            className={cn(
              "group relative flex flex-col items-start gap-1 border px-3 py-2 text-left transition-all",
              selected
                ? "border-primary/70 bg-primary/5 text-foreground shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.4)]"
                : "border-border/60 bg-background/40 text-muted-foreground hover:border-primary/40 hover:bg-accent/40 hover:text-foreground",
              (disabled || isLoading) && "cursor-not-allowed opacity-60",
            )}
          >
            <div className="flex w-full items-center justify-between">
              <span className="font-mono text-sm font-semibold">{key}</span>
              {selected ? (
                <Check className="h-3.5 w-3.5 text-primary" />
              ) : isLoading ? (
                <Loader2 className="h-3 w-3 animate-spin opacity-50" />
              ) : null}
            </div>
            {desc ? (
              <span className="line-clamp-2 text-[10px] leading-tight text-muted-foreground">
                {desc}
              </span>
            ) : (
              <span className="text-[10px] text-muted-foreground/50">
                {key === "gbm"
                  ? "Gradient-boosted predictor. Uses a bound model."
                  : key === "position_tracker"
                    ? "Track adopted/manual positions without submitting orders."
                  : key === "ma_crossover"
                    ? "Fast/slow moving-average crossover."
                    : "Allocate once, hold forever."}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
