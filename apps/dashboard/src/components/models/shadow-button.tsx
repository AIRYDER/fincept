"use client";

/**
 * ShadowButton — bind/unbind a model as the shadow candidate for an
 * agent (default ``gbm_predictor.v1``) — Phase E1/E3.
 *
 * Three states the operator might be looking at:
 *
 *   1. **This model is the active one.**
 *      No shadow action makes sense (the api refuses to shadow the
 *      same model that's active).  Render nothing -- the PromoteButton
 *      already shows the "Active" badge.
 *
 *   2. **This model is the current shadow.**
 *      Render a destructive-tinted "Clear shadow" button so the
 *      operator can stop the parallel inference work.
 *
 *   3. **This model is neither active nor shadow.**
 *      Render a primary-outline "Set as shadow" CTA.  Clicking it
 *      promotes this model to the shadow slot; the api response
 *      confirms by returning the new ``shadow`` binding.
 *
 * Mutations invalidate the same query keys as PromoteButton so the
 * model listing card and the promotion-history panel both pick up
 * the change immediately.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Eye,
  EyeOff,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface Props {
  /** Model name being viewed (the candidate or current shadow). */
  modelName: string;
  /** Default = ``gbm_predictor.v1``. */
  agentId?: string;
  /** Smaller variant for inline use (e.g. on the listing card). */
  compact?: boolean;
}

const DEFAULT_AGENT = "gbm_predictor.v1";

export function ShadowButton({ modelName, agentId, compact }: Props) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const aid = agentId ?? DEFAULT_AGENT;
  const [recentlyChanged, setRecentlyChanged] = useState(false);

  const state = useQuery({
    queryKey: ["models", "promote", aid],
    queryFn: () =>
      api.modelPromotionState(token, { agent_id: aid, history_limit: 1 }),
    enabled: !!token,
    staleTime: 5_000,
  });

  const setShadow = useMutation({
    mutationFn: () =>
      api.setShadow(token, modelName, {
        agent_id: aid,
        promoted_by: "operator",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      queryClient.invalidateQueries({ queryKey: ["models", "promote"] });
      setRecentlyChanged(true);
      window.setTimeout(() => setRecentlyChanged(false), 5_000);
    },
  });

  const clearShadow = useMutation({
    mutationFn: () =>
      api.clearShadow(token, { agent_id: aid, promoted_by: "operator" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      queryClient.invalidateQueries({ queryKey: ["models", "promote"] });
      setRecentlyChanged(true);
      window.setTimeout(() => setRecentlyChanged(false), 5_000);
    },
  });

  const isActive = state.data?.active?.model_name === modelName;
  const isShadow = state.data?.shadow?.model_name === modelName;

  const errorMsg = (() => {
    const err = setShadow.error ?? clearShadow.error;
    if (!err) return null;
    if (err instanceof ApiError) {
      const body = err.body;
      if (typeof body === "object" && body !== null && "detail" in body) {
        return String((body as { detail: unknown }).detail);
      }
      return err.message;
    }
    return String(err);
  })();

  // Active model: no shadow option is meaningful.
  if (isActive) {
    return null;
  }

  // Currently shadow: offer Clear.
  if (isShadow) {
    return (
      <div className="flex flex-col items-end gap-1">
        <Button
          size={compact ? "sm" : "default"}
          variant="outline"
          onClick={() => clearShadow.mutate()}
          disabled={clearShadow.isPending}
          className="gap-2 border-warn/40 text-warn hover:bg-warn/5"
        >
          {clearShadow.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <EyeOff className="h-3.5 w-3.5" />
          )}
          {clearShadow.isPending
            ? "Clearing…"
            : recentlyChanged
              ? "Cleared - hot-reload pending"
              : compact
                ? "Clear shadow"
                : "Clear shadow"}
        </Button>
        {recentlyChanged ? (
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <RefreshCw className="h-3 w-3" />
            <code className="font-mono">{aid.split(".")[0]}</code> will
            stop the shadow loop within ~30s.
          </div>
        ) : null}
        {errorMsg ? (
          <div className="flex items-center gap-1.5 text-[11px] text-destructive">
            <AlertTriangle className="h-3 w-3" />
            {errorMsg}
          </div>
        ) : null}
      </div>
    );
  }

  // Neither active nor shadow: offer Set as shadow.
  return (
    <div
      className={
        compact
          ? "flex flex-col items-stretch gap-1"
          : "flex flex-col items-end gap-1"
      }
    >
      <Button
        size={compact ? "sm" : "default"}
        variant="outline"
        onClick={() => setShadow.mutate()}
        disabled={setShadow.isPending}
        className="gap-2"
      >
        {setShadow.isPending ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Eye className="h-3.5 w-3.5" />
        )}
        {setShadow.isPending
          ? "Setting shadow…"
          : recentlyChanged
            ? "Shadow set - hot-reload pending"
            : compact
              ? "Shadow"
              : "Set as shadow"}
      </Button>
      {recentlyChanged ? (
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <RefreshCw className="h-3 w-3" />
          <code className="font-mono">{aid.split(".")[0]}</code> will
          start a parallel inference loop within ~30s.  Predictions are
          recorded to the JSONL log but NOT published.
        </div>
      ) : null}
      {errorMsg ? (
        <div className="flex items-center gap-1.5 text-[11px] text-destructive">
          <AlertTriangle className="h-3 w-3" />
          {errorMsg}
        </div>
      ) : null}
    </div>
  );
}
