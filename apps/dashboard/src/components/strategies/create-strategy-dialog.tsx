"use client";

/**
 * CreateStrategyDialog — modal form for ``POST /strategies/configs``.
 *
 * Composition
 * ~~~~~~~~~~~
 *
 * The form is split into three logical blocks with small section
 * headers:
 *
 *   1. Identity      -- strategy_id (immutable once created)
 *   2. Recipe        -- class_name + symbols
 *   3. Behaviour     -- params + optional model_binding + enabled
 *
 * Client-side validation is minimal on purpose: the store validates
 * ``strategy_id`` for filesystem safety and the api validates
 * ``class_name`` against the registry, so we defer to server errors
 * rather than duplicating the logic.  The only things we check
 * before the POST:
 *
 *   - strategy_id is non-empty and fits the same conservative
 *     character class the store will accept (lets the user see the
 *     error before the round-trip).
 *   - symbols has at least one entry (matches Pydantic
 *     ``min_length=1`` so the 422 wouldn't be instructive).
 *
 * Everything else flows through to the server for the
 * authoritative error message.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Fingerprint,
  Loader2,
  Play,
  Plus,
  SlidersHorizontal,
  Sparkles,
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
import type { CreateStrategyConfigBody } from "@/lib/types";
import { cn } from "@/lib/utils";

const ID_SAFE = /^[A-Za-z0-9][A-Za-z0-9_.\-]*$/;

interface FormState {
  strategy_id: string;
  class_name: string;
  symbols: string[];
  params: Record<string, unknown>;
  model_binding: string;
  enabled: boolean;
}

const DEFAULTS: FormState = {
  strategy_id: "",
  class_name: "ma_crossover",
  symbols: [],
  params: {},
  model_binding: "",
  enabled: false,
};

export function CreateStrategyDialog({
  trigger,
}: {
  /** Optional custom trigger; defaults to a primary button. */
  trigger?: React.ReactNode;
}) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(DEFAULTS);

  // Reset on open so reopening after a cancel doesn't keep stale state.
  useEffect(() => {
    if (open) setForm(DEFAULTS);
  }, [open]);

  const mutation = useMutation({
    mutationFn: (body: CreateStrategyConfigBody) =>
      api.createStrategyConfig(token, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      queryClient.invalidateQueries({ queryKey: ["strategies", "configs"] });
      setOpen(false);
      setForm(DEFAULTS);
    },
  });

  const errorMessage = apiErrorMessage(mutation.error);
  const idValid =
    form.strategy_id.length > 0 && ID_SAFE.test(form.strategy_id);
  const canSubmit =
    idValid && form.symbols.length > 0 && !!form.class_name;

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const body: CreateStrategyConfigBody = {
      strategy_id: form.strategy_id.trim(),
      class_name: form.class_name,
      symbols: form.symbols,
      params: form.params,
      model_binding: form.model_binding.trim() || null,
      enabled: form.enabled,
    };
    mutation.mutate(body);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" className="gap-2">
            <Plus className="h-3.5 w-3.5" />
            New strategy
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="max-h-[92vh] overflow-y-auto scrollbar-thin sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            New strategy
          </DialogTitle>
          <DialogDescription>
            Persists a StrategyConfig.  The strategy-host supervisor
            picks it up on its next reconcile tick.  ``Enabled`` defaults
            off so you can edit before going live.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={onSubmit} className="space-y-5">
          {/* --- Identity ----------------------------------------------- */}
          <Section
            icon={Fingerprint}
            title="Identity"
            hint="This becomes the strategy_id stamped on every OrderIntent.  Not editable later — rename means DELETE + create."
          >
            <Input
              value={form.strategy_id}
              onChange={(e) =>
                setForm((s) => ({ ...s, strategy_id: e.target.value }))
              }
              placeholder="btc_ma_main"
              autoComplete="off"
              autoFocus
              required
              className={cn(
                "font-mono",
                form.strategy_id.length > 0 && !idValid
                  ? "border-destructive/60 focus-visible:ring-destructive/50"
                  : null,
              )}
            />
            {form.strategy_id.length > 0 && !idValid ? (
              <p className="mt-1 text-[11px] text-destructive">
                Must start with a letter/digit and contain only letters,
                digits, dot, dash, or underscore.
              </p>
            ) : (
              <p className="mt-1 text-[10px] text-muted-foreground">
                Allowed: letters, digits, dot, dash, underscore.
              </p>
            )}
          </Section>

          {/* --- Recipe ------------------------------------------------ */}
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

          {/* --- Behaviour --------------------------------------------- */}
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
                  Name of the agent whose active model this strategy reloads
                  when you promote.  Ignored by non-ML strategies.
                </p>
              </div>
              <div>
                <Label>Initial state</Label>
                <EnabledToggle
                  value={form.enabled}
                  onChange={(enabled) => setForm((s) => ({ ...s, enabled }))}
                />
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
              <kbd className="rounded border border-border/60 bg-background/60 px-1 py-0 font-mono text-[10px]">
                Esc
              </kbd>{" "}
              to cancel
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
                ) : form.enabled ? (
                  <Play className="h-3.5 w-3.5" />
                ) : (
                  <Plus className="h-3.5 w-3.5" />
                )}
                {mutation.isPending
                  ? "Creating…"
                  : form.enabled
                    ? "Create & start"
                    : "Create"}
              </Button>
            </div>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- //
// Internal layout helpers                                                     //
// --------------------------------------------------------------------------- //

function Section({
  icon: Icon,
  title,
  hint,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  hint?: string;
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
      {hint ? (
        <p className="text-[10px] text-muted-foreground">{hint}</p>
      ) : null}
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

function EnabledToggle({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="inline-flex items-stretch overflow-hidden rounded-md border border-border">
      <button
        type="button"
        onClick={() => onChange(false)}
        className={cn(
          "inline-flex items-center gap-2 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest transition-colors",
          !value
            ? "bg-muted/60 text-foreground"
            : "text-muted-foreground hover:bg-accent/50",
        )}
      >
        Disabled
      </button>
      <button
        type="button"
        onClick={() => onChange(true)}
        className={cn(
          "inline-flex items-center gap-2 border-l border-border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest transition-colors",
          value
            ? "bg-long/15 text-long"
            : "text-muted-foreground hover:bg-long/10 hover:text-long",
        )}
      >
        <Play className="h-3 w-3" />
        Enabled
      </button>
    </div>
  );
}
