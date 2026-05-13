"use client";

/**
 * ModelBindingChip — compact indicator of a strategy's bound agent
 * and whether that agent has a currently-active model promoted.
 *
 * Why this widget exists
 * ~~~~~~~~~~~~~~~~~~~~~~
 *
 * A ``gbm`` strategy config carries a ``model_binding`` string like
 * ``gbm_predictor.v1``.  That string is only meaningful once an
 * operator has *promoted* a trained model for that agent; until
 * then the strategy host falls back to the default model dir with
 * no guarantee the strategy will actually produce predictions.  An
 * operator scanning the list needs to see two things at once:
 *
 *   1. What agent is this strategy bound to?  (the string itself)
 *   2. Does that agent have an active model?  (the health dot)
 *
 * The chip lazily queries ``/models/promote/active?agent_id=…`` and
 * caches the result; the ``enabled: !!modelBinding`` guard means an
 * unbound strategy never fires the request.
 */

import { useQuery } from "@tanstack/react-query";
import { Brain, Link2, Unlink } from "lucide-react";
import Link from "next/link";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

export function ModelBindingChip({
  modelBinding,
  compact = false,
  className,
}: {
  modelBinding: string | null;
  compact?: boolean;
  className?: string;
}) {
  const token = useAuth((s) => s.token);

  const promotion = useQuery({
    queryKey: ["models", "promote", modelBinding],
    queryFn: () =>
      api.modelPromotionState(token, {
        agent_id: modelBinding ?? "",
        history_limit: 1,
      }),
    enabled: !!token && !!modelBinding,
    staleTime: 30_000,
  });

  if (!modelBinding) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 border border-dashed border-border/60 bg-background/30 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground",
          className,
        )}
        title="No model binding — strategy will use defaults only"
      >
        <Unlink className="h-3 w-3" />
        unbound
      </span>
    );
  }

  const active = promotion.data?.active?.model_name ?? null;
  const shadow = promotion.data?.shadow?.model_name ?? null;
  const healthy = active != null;

  const label = compact ? modelBinding.split(".")[0] : modelBinding;

  return (
    <Link
      href="/models"
      className={cn(
        "group inline-flex items-center gap-1.5 border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider transition-colors",
        healthy
          ? "border-long/40 bg-long/5 text-long hover:border-long/70 hover:bg-long/10"
          : "border-warn/40 bg-warn/5 text-warn hover:border-warn/70 hover:bg-warn/10",
        className,
      )}
      title={
        healthy
          ? `Bound to ${modelBinding} · active model: ${active}${
              shadow ? ` · shadow: ${shadow}` : ""
            }`
          : `Bound to ${modelBinding} · no active model promoted yet`
      }
    >
      {healthy ? (
        <Brain className="h-3 w-3" />
      ) : (
        <Link2 className="h-3 w-3" />
      )}
      <span className="truncate">{label}</span>
      {healthy && !compact ? (
        <span className="text-[9px] opacity-70">· {active}</span>
      ) : null}
    </Link>
  );
}
