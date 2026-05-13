"use client";

/**
 * EditStrategyDialog — PATCH form for an existing strategy config.
 *
 * Mirrors the CreateStrategyDialog layout so muscle-memory carries,
 * minus the ``strategy_id`` field (URL key, not editable).  Submits
 * a *partial* body -- only fields the user actually touched, so
 * unchanged params don't stomp a concurrent edit from another
 * window.
 *
 * Change detection uses a shallow equality against the original
 * config; any edit to a field (even a null->null no-op clear)
 * includes that field in the PATCH.  This matches the UX
 * expectation that clicking "Save" saves exactly what's visible
 * and nothing more.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Loader2,
  Pencil,
  Save,
  SlidersHorizontal,
  Workflow,
} from "lucide-react";
import { useEffect, useState } from "react";

import { ClassPicker } from "@/components/strategies/class-picker";
import { ParamsEditor } from "@/components/strategies/params-editor";
import { SymbolsInput } from "@/components/strategies/symbols-input";
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
import type { StrategyConfigRow, UpdateStrategyConfigBody } from "@/lib/types";
import { cn } from "@/lib/utils";

interface FormState {
  class_name: string;
  symbols: string[];
  params: Record<string, unknown>;
  model_binding: string;
}

function fromConfig(c: StrategyConfigRow): FormState {
  return {
    class_name: c.class_name,
    symbols: [...c.symbols],
    params: { ...c.params },
    model_binding: c.model_binding ?? "",
  };
}

export function EditStrategyDialog({
  config,
  trigger,
  open: externalOpen,
  onOpenChange,
}: {
  config: StrategyConfigRow;
  trigger?: React.ReactNode;
  /** Optional controlled-open for parent-driven edit triggers. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [internalOpen, setInternalOpen] = useState(false);
  const open = externalOpen ?? internalOpen;
  const setOpen = onOpenChange ?? setInternalOpen;

  const [form, setForm] = useState<FormState>(() => fromConfig(config));

  // Reset form when the dialog opens or the underlying config ref
  // changes (e.g. parent refetched and passed a newer snapshot).
  useEffect(() => {
    if (open) setForm(fromConfig(config));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, config.strategy_id, config.updated_at]);

  const mutation = useMutation({
    mutationFn: (body: UpdateStrategyConfigBody) =>
      api.updateStrategyConfig(token, config.strategy_id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      queryClient.invalidateQueries({ queryKey: ["strategies", "configs"] });
      queryClient.invalidateQueries({
        queryKey: ["strategies", "configs", config.strategy_id],
      });
      queryClient.invalidateQueries({
        queryKey: ["strategies", "history", config.strategy_id],
      });
      setOpen(false);
    },
  });

  const errorMessage = apiErrorMessage(mutation.error);

  // Compute the minimal patch -- server treats absent keys as "leave
  // alone", so we only send fields the user changed.
  const buildPatch = (): UpdateStrategyConfigBody => {
    const patch: UpdateStrategyConfigBody = {};
    if (form.class_name !== config.class_name) {
      patch.class_name = form.class_name;
    }
    if (!sameList(form.symbols, config.symbols)) {
      patch.symbols = form.symbols;
    }
    if (JSON.stringify(form.params) !== JSON.stringify(config.params)) {
      patch.params = form.params;
    }
    const newBinding = form.model_binding.trim() || null;
    if (newBinding !== (config.model_binding ?? null)) {
      patch.model_binding = newBinding;
    }
    return patch;
  };

  const patch = buildPatch();
  const hasChanges = Object.keys(patch).length > 0;
  const canSubmit = form.symbols.length > 0 && !!form.class_name && hasChanges;

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    mutation.mutate(patch);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {trigger !== undefined ? (
        <DialogTrigger asChild>{trigger}</DialogTrigger>
      ) : null}
      <DialogContent className="max-h-[92vh] overflow-y-auto scrollbar-thin sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Pencil className="h-4 w-4 text-primary" />
            Edit{" "}
            <code className="ml-1 font-mono text-sm text-foreground/80">
              {config.strategy_id}
            </code>
          </DialogTitle>
          <DialogDescription>
            Only the fields you change are sent; unchanged fields are
            left exactly as they are on the server.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} className="space-y-5">
          <Section icon={Workflow} title="Recipe">
            <div className="space-y-3">
              <div>
                <Label>Class</Label>
                <ClassPicker
                  value={form.class_name}
                  onChange={(class_name) =>
                    setForm((s) => ({ ...s, class_name }))
                  }
                />
              </div>
              <div>
                <Label>Symbols</Label>
                <SymbolsInput
                  value={form.symbols}
                  onChange={(symbols) => setForm((s) => ({ ...s, symbols }))}
                />
              </div>
            </div>
          </Section>

          <Section icon={SlidersHorizontal} title="Behaviour">
            <div className="space-y-3">
              <div>
                <Label>Params</Label>
                <ParamsEditor
                  value={form.params}
                  onChange={(params) => setForm((s) => ({ ...s, params }))}
                  classKey={form.class_name}
                />
              </div>
              <div>
                <Label>
                  Model binding{" "}
                  <span className="text-muted-foreground/60">(optional)</span>
                </Label>
                <Input
                  value={form.model_binding}
                  onChange={(e) =>
                    setForm((s) => ({ ...s, model_binding: e.target.value }))
                  }
                  placeholder={
                    form.class_name === "gbm"
                      ? "gbm_predictor.v1"
                      : "(only used by the gbm strategy)"
                  }
                  className="font-mono"
                />
                <p className="mt-1 text-[10px] text-muted-foreground">
                  Clearing this (empty) unbinds — the strategy host falls
                  back to the default model dir on the next hot-reload.
                </p>
              </div>
            </div>
          </Section>

          {errorMessage ? (
            <div
              className={cn(
                "flex items-start gap-2 border px-3 py-2 text-xs",
                errorMessage.tone === "warn"
                  ? "border-warn/40 bg-warn/5 text-warn"
                  : "border-destructive/40 bg-destructive/5 text-destructive",
              )}
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{errorMessage.text}</span>
            </div>
          ) : null}

          <div className="flex items-center justify-between border-t border-border/40 pt-3">
            <p className="text-[10px] text-muted-foreground">
              {hasChanges
                ? `${Object.keys(patch).length} field(s) changed`
                : "No changes"}
            </p>
            <div className="flex gap-2">
              <DialogClose asChild>
                <Button type="button" variant="ghost" size="sm">
                  Cancel
                </Button>
              </DialogClose>
              <Button
                type="submit"
                size="sm"
                disabled={!canSubmit || mutation.isPending}
                className="gap-2"
              >
                {mutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Save className="h-3.5 w-3.5" />
                )}
                {mutation.isPending ? "Saving…" : "Save changes"}
              </Button>
            </div>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function sameList(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function Section({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 border-b border-border/40 pb-1">
        <Icon className="h-3.5 w-3.5 text-cyan" />
        <span className="text-[10px] font-semibold uppercase tracking-widest text-cyan">
          {title}
        </span>
      </div>
      {children}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <span className="mb-1 block text-[10px] uppercase tracking-widest text-muted-foreground">
      {children}
    </span>
  );
}
