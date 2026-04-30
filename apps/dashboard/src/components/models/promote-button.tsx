"use client";

/**
 * PromoteButton - one-click promotion of a trained model to the
 * active-model pointer for an agent (default ``gbm_predictor.v1``).
 *
 * Visual states:
 *
 *   - This model is *not* the active one      -> primary "Promote" CTA
 *   - This model *is* the active one           -> disabled ✓ "Active"
 *     (with a tooltip explaining what active means)
 *   - Mutation in flight                       -> spinner
 *   - Mutation just succeeded                  -> short-lived "promoted ✓"
 *     state with a "hot-reload pending" hint
 *   - Mutation errored                         -> inline destructive text
 *
 * On success we invalidate the promotion-state and models queries so
 * the active badge on the listing page repaints immediately.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Rocket,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

interface Props {
  /** Model name being viewed (the candidate for promotion). */
  modelName: string;
  /** Default = ``gbm_predictor.v1`` (only ML-backed agent today). */
  agentId?: string;
  /** Render as a smaller variant for inline use on the listing card. */
  compact?: boolean;
}

const DEFAULT_AGENT = "gbm_predictor.v1";

export function PromoteButton({ modelName, agentId, compact }: Props) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const aid = agentId ?? DEFAULT_AGENT;
  const [recentlyPromoted, setRecentlyPromoted] = useState(false);

  const state = useQuery({
    queryKey: ["models", "promote", aid],
    queryFn: () =>
      api.modelPromotionState(token, { agent_id: aid, history_limit: 1 }),
    enabled: !!token,
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: () =>
      api.promoteModel(token, modelName, {
        agent_id: aid,
        promoted_by: "operator",
      }),
    onSuccess: () => {
      // The active state, model listing (which surfaces the badge),
      // and history panel all need to re-fetch.
      queryClient.invalidateQueries({ queryKey: ["models"] });
      queryClient.invalidateQueries({ queryKey: ["models", "promote"] });
      setRecentlyPromoted(true);
      // Auto-clear the success banner after a few seconds; the active
      // state will have updated by then so the disabled "Active"
      // variant takes over.
      window.setTimeout(() => setRecentlyPromoted(false), 5_000);
    },
  });

  const isActive = state.data?.active?.model_name === modelName;
  const errorMsg = (() => {
    if (!mutation.error) return null;
    if (mutation.error instanceof ApiError) {
      const detail =
        typeof mutation.error.body === "object" &&
        mutation.error.body !== null &&
        "detail" in mutation.error.body
          ? String((mutation.error.body as { detail: unknown }).detail)
          : mutation.error.message;
      return detail;
    }
    return String(mutation.error);
  })();

  if (isActive && !recentlyPromoted) {
    return (
      <Button
        size={compact ? "sm" : "default"}
        variant="outline"
        disabled
        className="gap-2 border-long/40 bg-long/5 text-long"
        title={`Active model for ${aid}`}
      >
        <CheckCircle2 className="h-3.5 w-3.5" />
        Active
      </Button>
    );
  }

  return (
    <div className={cn("flex flex-col gap-1", compact ? "items-stretch" : "items-end")}>
      <Button
        size={compact ? "sm" : "default"}
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        className="gap-2"
      >
        {mutation.isPending ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Rocket className="h-3.5 w-3.5" />
        )}
        {mutation.isPending
          ? "Promoting…"
          : recentlyPromoted
            ? "Promoted - hot-reload pending"
            : compact
              ? "Promote"
              : "Promote to active"}
      </Button>
      {recentlyPromoted ? (
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <RefreshCw className="h-3 w-3" />
          <code className="font-mono">{aid.split(".")[0]}</code> will
          hot-reload within ~30s.
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
