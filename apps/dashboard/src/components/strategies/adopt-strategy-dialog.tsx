"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, ShieldCheck } from "lucide-react";
import { type ReactNode, useState } from "react";

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
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function AdoptStrategyDialog({
  strategyId,
  symbols,
  trigger,
}: {
  strategyId: string;
  symbols: string[];
  trigger?: ReactNode;
}) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const mutation = useMutation({
    mutationFn: () => api.adoptStrategyConfig(token, strategyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      queryClient.invalidateQueries({ queryKey: ["strategies", "configs"] });
      setOpen(false);
    },
  });

  const errorMessage = apiErrorMessage(mutation.error);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button variant="outline" size="sm" className="gap-2">
            <ShieldCheck className="h-3.5 w-3.5" />
            Adopt
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-warn" />
            Adopt orphan strategy
          </DialogTitle>
          <DialogDescription>
            Create a disabled position-tracker config for this runtime-only
            strategy so it can be edited, audited, and deliberately started
            later.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="border border-warn/30 bg-warn/5 p-3">
            <div className="flex items-start gap-2 text-warn">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <div className="space-y-1 text-[11px] leading-relaxed">
                <p>
                  Adoption does not place orders, close positions, or enable a
                  runner. It only saves the current open symbols under a
                  disabled <code className="font-mono">position_tracker</code>
                  config.
                </p>
              </div>
            </div>
          </div>

          <div className="space-y-2 text-xs">
            <div className="flex items-center justify-between border border-border/50 bg-background/30 px-3 py-2">
              <span className="text-muted-foreground">Strategy</span>
              <code className="font-mono text-foreground">{strategyId}</code>
            </div>
            <div className="flex items-center justify-between border border-border/50 bg-background/30 px-3 py-2">
              <span className="text-muted-foreground">Open symbols</span>
              <span className="font-mono text-foreground">
                {symbols.length ? symbols.join(", ") : "none"}
              </span>
            </div>
          </div>

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
              size="sm"
              disabled={mutation.isPending || symbols.length === 0}
              onClick={() => mutation.mutate()}
              className="gap-2"
            >
              {mutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <ShieldCheck className="h-3.5 w-3.5" />
              )}
              {mutation.isPending ? "Adopting…" : "Adopt safely"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
