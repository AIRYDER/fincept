"use client";

/**
 * DeleteStrategyDialog — destructive confirmation with type-to-match.
 *
 * Deleting a strategy removes the active config on disk but the
 * history JSONL is kept for forensic use.  Because the action can't
 * be undone from the UI (the operator has to re-POST the config),
 * we guard it with a type-to-confirm challenge: the button only
 * enables after the user types the strategy_id exactly.
 *
 * The pattern is standard for destructive ops in serious tools
 * (GitHub repo deletion, AWS cluster termination); it adds 2s of
 * friction but prevents the common "oops, wrong row" mistake on a
 * dense list.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiErrorMessage } from "@/components/strategies/use-api-error";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { StrategyConfigRow } from "@/lib/types";
import { cn } from "@/lib/utils";

export function DeleteStrategyDialog({
  config,
  trigger,
  /** When true, redirect to /strategies on success (detail page). */
  redirectOnSuccess = false,
}: {
  config: StrategyConfigRow;
  trigger?: React.ReactNode;
  redirectOnSuccess?: boolean;
}) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState("");

  useEffect(() => {
    if (open) setConfirm("");
  }, [open]);

  const mutation = useMutation({
    mutationFn: () => api.deleteStrategyConfig(token, config.strategy_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      queryClient.invalidateQueries({ queryKey: ["strategies", "configs"] });
      // Drop the per-id caches too so a future navigation re-fetches.
      queryClient.removeQueries({
        queryKey: ["strategies", "configs", config.strategy_id],
      });
      queryClient.removeQueries({
        queryKey: ["strategies", "history", config.strategy_id],
      });
      setOpen(false);
      if (redirectOnSuccess) {
        router.push("/strategies");
      }
    },
  });

  const errorMessage = apiErrorMessage(mutation.error);
  const canConfirm = confirm === config.strategy_id;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button variant="outline" size="sm" className="gap-2">
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <AlertTriangle className="h-4 w-4" />
            Delete strategy
          </DialogTitle>
          <DialogDescription>
            This removes the active config on disk.  The audit history
            (<code className="font-mono text-[11px]">
              {config.strategy_id}.history.jsonl
            </code>
            ) is retained so the timeline is still inspectable.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="border border-destructive/30 bg-destructive/5 p-3">
            <div className="flex items-start gap-2 text-destructive">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <div className="space-y-1 text-[11px] leading-relaxed">
                <p>
                  The strategy host supervisor will cancel this runner
                  on its next reconcile tick.  Open positions are{" "}
                  <strong className="font-semibold">not</strong>{" "}
                  flattened — use OMS kill-switch or manual close orders
                  if you need that.
                </p>
                <p>
                  Re-POSTing the same strategy_id later resurrects the
                  config, but the interval between delete and re-create
                  will show as a gap in the audit timeline.
                </p>
              </div>
            </div>
          </div>

          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
              Type{" "}
              <code className="rounded bg-muted/40 px-1 font-mono text-[11px] text-foreground">
                {config.strategy_id}
              </code>{" "}
              to confirm
            </span>
            <Input
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder={config.strategy_id}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              className={cn(
                "font-mono",
                confirm.length > 0 && !canConfirm
                  ? "border-destructive/40"
                  : null,
                canConfirm ? "border-destructive/70" : null,
              )}
            />
          </label>

          {errorMessage ? (
            <div className="flex items-start gap-2 border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{errorMessage.text}</span>
            </div>
          ) : null}

          <div className="flex justify-end gap-2 border-t border-border/40 pt-3">
            <DialogClose asChild>
              <Button type="button" variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={!canConfirm || mutation.isPending}
              onClick={() => mutation.mutate()}
              className="gap-2"
            >
              {mutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )}
              {mutation.isPending ? "Deleting…" : "Delete forever"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
