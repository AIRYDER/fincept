"use client";

/**
 * PromotionHistoryPanel - audit trail of which model was active when.
 *
 * Each row shows the model name, who promoted it, and how long ago.
 * The most-recent row carries an "ACTIVE" badge.  A rollback button
 * lives at the top of the panel and pops the latest entry off the
 * stack (see ``PromotionStore.rollback`` for exact semantics).
 *
 * Empty state: an explanatory blurb pointing the operator at the
 * Promote button on a model detail page, since rolling back from
 * an empty history is a no-op.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  CheckCircle2,
  History,
  Loader2,
  RefreshCw,
  Undo2,
} from "lucide-react";
import { useState } from "react";

import { EmptyState } from "@/components/widgets/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { ActiveBinding } from "@/lib/types";

const DEFAULT_AGENT = "gbm_predictor.v1";

export function PromotionHistoryPanel({
  agentId = DEFAULT_AGENT,
}: { agentId?: string } = {}) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [showReload, setShowReload] = useState(false);

  const state = useQuery({
    queryKey: ["models", "promote", agentId],
    queryFn: () =>
      api.modelPromotionState(token, {
        agent_id: agentId,
        history_limit: 20,
      }),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 5_000,
  });

  const rollback = useMutation({
    mutationFn: () =>
      api.rollbackPromotion(token, {
        agent_id: agentId,
        promoted_by: "operator",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      queryClient.invalidateQueries({ queryKey: ["models", "promote"] });
      setShowReload(true);
      window.setTimeout(() => setShowReload(false), 5_000);
    },
  });

  const errorMsg = (() => {
    if (!rollback.error) return null;
    if (rollback.error instanceof ApiError) {
      const detail =
        typeof rollback.error.body === "object" &&
        rollback.error.body !== null &&
        "detail" in rollback.error.body
          ? String((rollback.error.body as { detail: unknown }).detail)
          : rollback.error.message;
      return detail;
    }
    return String(rollback.error);
  })();

  const history = state.data?.history ?? [];
  const active = state.data?.active ?? null;
  const shadow = state.data?.shadow ?? null;

  return (
    <Card className="mt-6">
      <CardHeader className="flex flex-row items-start justify-between gap-3 pb-3">
        <div>
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <History className="h-4 w-4 text-primary" />
            Promotion history
          </CardTitle>
          <CardDescription>
            Which model was active for{" "}
            <code className="font-mono text-[11px]">{agentId}</code>, and
            when.  Rollback restores the previous binding; the agent
            picks up the change automatically within ~30s.
          </CardDescription>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => rollback.mutate()}
          disabled={rollback.isPending || history.length === 0}
          className="gap-2"
        >
          {rollback.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Undo2 className="h-3.5 w-3.5" />
          )}
          Rollback
        </Button>
      </CardHeader>
      <CardContent>
        {shadow ? (
          <div className="mb-3 flex items-center justify-between gap-3 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-xs">
            <div className="flex min-w-0 items-center gap-2">
              <span className="shrink-0 rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-widest text-warn">
                Shadow
              </span>
              <code className="truncate font-mono text-warn">
                {shadow.model_name}
              </code>
              <span className="shrink-0 text-muted-foreground">
                set by {shadow.promoted_by}
              </span>
            </div>
            <span className="shrink-0 text-[10px] text-muted-foreground">
              recording predictions, not publishing
            </span>
          </div>
        ) : null}
        {showReload ? (
          <div className="mb-3 flex items-center gap-2 rounded-md border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            <RefreshCw className="h-3.5 w-3.5" />
            Rolled back.{" "}
            <code className="font-mono">
              {agentId.split(".")[0]}
            </code>{" "}
            will hot-reload within ~30s.
          </div>
        ) : null}
        {errorMsg ? (
          <div className="mb-3 flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            <AlertTriangle className="h-3.5 w-3.5" />
            {errorMsg}
          </div>
        ) : null}

        {state.error ? (
          <EmptyState
            icon={AlertTriangle}
            title="Failed to load history"
            description={
              state.error instanceof Error
                ? state.error.message
                : "Unknown error"
            }
          />
        ) : state.isLoading ? (
          <EmptyState
            icon={History}
            title="Loading history…"
            description="Polling /models/promote/active."
          />
        ) : history.length === 0 ? (
          <EmptyState
            icon={History}
            title="No promotions yet"
            description={`No model has been promoted to ${agentId}. Click "Promote" on a trained model's detail page to bind it.`}
          />
        ) : (
          <ol className="divide-y divide-border/30 rounded-md border border-border/40 bg-background/30">
            {history.map((row, idx) => (
              <HistoryRow
                key={`${row.model_name}-${row.promoted_at}-${idx}`}
                row={row}
                isCurrentActive={
                  active != null &&
                  row.model_name === active.model_name &&
                  row.promoted_at === active.promoted_at
                }
              />
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

function HistoryRow({
  row,
  isCurrentActive,
}: {
  row: ActiveBinding;
  isCurrentActive: boolean;
}) {
  const ageLabel = formatRelativeAge(row.promoted_at);
  const isClear = row.model_name === "(rolled-back-to-empty)";
  return (
    <motion.li
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.2 }}
      className="flex items-center gap-3 px-3 py-2 text-sm"
    >
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-border/40 bg-background/40">
        {isCurrentActive ? (
          <CheckCircle2 className="h-3.5 w-3.5 text-long" />
        ) : isClear ? (
          <Undo2 className="h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <History className="h-3.5 w-3.5 text-muted-foreground" />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={
              isClear
                ? "italic text-muted-foreground"
                : "font-mono text-xs font-semibold"
            }
          >
            {isClear ? "rolled back to empty" : row.model_name}
          </span>
          {isCurrentActive ? (
            <Badge
              variant="long"
              className="font-mono uppercase tracking-widest"
            >
              Active
            </Badge>
          ) : null}
        </div>
        <div className="flex items-baseline gap-2 text-[11px] text-muted-foreground">
          <span>by {row.promoted_by}</span>
          <span>·</span>
          <span>{ageLabel}</span>
        </div>
      </div>
    </motion.li>
  );
}

function formatRelativeAge(unixSeconds: number): string {
  const delta = Date.now() / 1000 - unixSeconds;
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}
