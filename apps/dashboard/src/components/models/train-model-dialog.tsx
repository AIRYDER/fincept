"use client";

/**
 * TrainModelDialog -- modal form that POSTs to /models/train.
 *
 * Form-state philosophy:
 *
 *   The dashboard is an operator tool, not a public form, so we
 *   prioritise speed over hand-holding.  Required fields are
 *   ``model_name`` and ``input_path``; everything else has a sensible
 *   default that matches the trainer CLI default (so "click Train,
 *   change nothing" produces the same model the CLI would).
 *
 * On success we don't navigate -- the runs panel right below polls
 * fast enough that the new run pops in within a second or two.
 *
 * On 429 we surface the operator-friendly message ("already N run(s)
 * in flight") inline rather than as a generic toast, since it's a
 * recoverable error the user can act on (wait, then retry).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Play, X } from "lucide-react";
import { useState } from "react";

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
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { TrainModelBody } from "@/lib/types";

interface TrainModelDialogProps {
  initialBody?: Partial<TrainModelBody>;
  triggerLabel?: string;
}

interface FormState {
  model_name: string;
  input_path: string;
  horizon_bars: string;
  bar_seconds: string;
  cv_folds: string;
  purge_bars: string;
  embargo_bars: string;
  num_boost_round: string;
  early_stopping_rounds: string;
}

const DEFAULTS: FormState = {
  model_name: "",
  input_path: "data/synth_bars.parquet",
  horizon_bars: "15",
  bar_seconds: "60",
  cv_folds: "5",
  purge_bars: "-1",
  embargo_bars: "0",
  num_boost_round: "500",
  early_stopping_rounds: "30",
};

const TRAINING_PRESETS: Array<{
  label: string;
  description: string;
  prefix: string;
  body: Omit<TrainModelBody, "model_name">;
}> = [
  {
    label: "GBM synthetic CV",
    description: "Known-good feature parquet with 5-fold walk-forward CV.",
    prefix: "gbm_synth",
    body: {
      input_path: "data/synth_bars.parquet",
      horizon_bars: 15,
      bar_seconds: 60,
      cv_folds: 5,
      purge_bars: -1,
      embargo_bars: 0,
      num_boost_round: 500,
      early_stopping_rounds: 30,
    },
  },
  {
    label: "Fast smoke train",
    description: "Quick holdout run for validating the pipeline path.",
    prefix: "gbm_smoke",
    body: {
      input_path: "data/synth_bars.parquet",
      horizon_bars: 15,
      bar_seconds: 60,
      cv_folds: 0,
      purge_bars: -1,
      embargo_bars: 0,
      num_boost_round: 100,
      early_stopping_rounds: 15,
    },
  },
];

export function TrainModelDialog({
  initialBody,
  triggerLabel = "Train new model",
}: TrainModelDialogProps) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(() => bodyToForm(initialBody));

  const mutation = useMutation({
    mutationFn: (body: TrainModelBody) => api.trainModel(token, body),
    onSuccess: () => {
      // The runs list polls but we kick a refetch immediately so the
      // user sees their submission within the same render.
      queryClient.invalidateQueries({ queryKey: ["models", "runs"] });
      queryClient.invalidateQueries({ queryKey: ["models"] });
      // Reset and close.
      setForm(bodyToForm(initialBody));
      setOpen(false);
    },
  });

  const errorMessage = (() => {
    const err = mutation.error;
    if (!err) return null;
    if (err instanceof ApiError) {
      const detail =
        typeof err.body === "object" && err.body !== null && "detail" in err.body
          ? String((err.body as { detail: unknown }).detail)
          : err.message;
      // Tag rate-limit errors so the alert is colored differently.
      if (err.status === 429) {
        return { tone: "warn" as const, text: detail };
      }
      return { tone: "danger" as const, text: detail };
    }
    return { tone: "danger" as const, text: String(err) };
  })();

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const body: TrainModelBody = {
      model_name: form.model_name.trim(),
      input_path: form.input_path.trim(),
      horizon_bars: Number.parseInt(form.horizon_bars, 10),
      bar_seconds: Number.parseInt(form.bar_seconds, 10),
      cv_folds: Number.parseInt(form.cv_folds, 10),
      purge_bars: Number.parseInt(form.purge_bars, 10),
      embargo_bars: Number.parseInt(form.embargo_bars, 10),
      num_boost_round: Number.parseInt(form.num_boost_round, 10),
      early_stopping_rounds: Number.parseInt(form.early_stopping_rounds, 10),
    };
    mutation.mutate(body);
  };

  const applyPreset = (preset: (typeof TRAINING_PRESETS)[number]) => {
    setForm((s) => ({
      ...bodyToForm({
        model_name: s.model_name.trim() || suggestedModelName(preset.prefix),
        ...preset.body,
      }),
    }));
    mutation.reset();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setForm(bodyToForm(initialBody));
          mutation.reset();
        }
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" className="gap-2">
          <Play className="h-3.5 w-3.5" />
          {triggerLabel}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Train a new model</DialogTitle>
          <DialogDescription>
            Spawns the GBM trainer in a subprocess.  The api streams
            stdout to a log file you can tail from the runs panel.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {TRAINING_PRESETS.map((preset) => (
              <button
                key={preset.label}
                type="button"
                onClick={() => applyPreset(preset)}
                className="rounded-md border border-border/50 bg-background/30 p-3 text-left transition-colors hover:border-primary/60 hover:bg-primary/5"
              >
                <div className="text-[11px] font-semibold uppercase tracking-widest text-primary">
                  {preset.label}
                </div>
                <div className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
                  {preset.description}
                </div>
                <code className="mt-2 block truncate font-mono text-[10px] text-muted-foreground">
                  {preset.body.input_path}
                </code>
              </button>
            ))}
          </div>

          {initialBody?.input_path ? (
            <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-[11px] text-muted-foreground">
              Prefilled from existing model path:{" "}
              <code className="font-mono text-primary">
                {initialBody.input_path}
              </code>
            </div>
          ) : null}

          <Field
            label="Model name"
            hint="Letters, digits, dot, underscore, dash.  Becomes the directory under MODELS_DIR."
          >
            <Input
              value={form.model_name}
              onChange={(e) =>
                setForm((s) => ({ ...s, model_name: e.target.value }))
              }
              placeholder="gbm_predictor_2024q4"
              autoFocus
              required
            />
          </Field>

          <Field
            label="Input parquet path"
            hint="Server-side path the api can read.  Must contain FEATURES + 'close' columns."
          >
            <Input
              value={form.input_path}
              onChange={(e) =>
                setForm((s) => ({ ...s, input_path: e.target.value }))
              }
              placeholder="data/synth_bars.parquet"
              required
            />
          </Field>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Field label="Horizon (bars)">
              <Input
                type="number"
                min={1}
                max={10000}
                value={form.horizon_bars}
                onChange={(e) =>
                  setForm((s) => ({ ...s, horizon_bars: e.target.value }))
                }
              />
            </Field>
            <Field label="Bar seconds">
              <Input
                type="number"
                min={1}
                max={86400}
                value={form.bar_seconds}
                onChange={(e) =>
                  setForm((s) => ({ ...s, bar_seconds: e.target.value }))
                }
              />
            </Field>
            <Field label="CV folds" hint="0 = legacy 80/20 holdout">
              <Input
                type="number"
                min={0}
                max={50}
                value={form.cv_folds}
                onChange={(e) =>
                  setForm((s) => ({ ...s, cv_folds: e.target.value }))
                }
              />
            </Field>
            <Field label="Boost rounds">
              <Input
                type="number"
                min={1}
                max={100000}
                value={form.num_boost_round}
                onChange={(e) =>
                  setForm((s) => ({ ...s, num_boost_round: e.target.value }))
                }
              />
            </Field>
            <Field label="Early stopping">
              <Input
                type="number"
                min={1}
                max={10000}
                value={form.early_stopping_rounds}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    early_stopping_rounds: e.target.value,
                  }))
                }
              />
            </Field>
          </div>

          {errorMessage ? (
            <div
              className={`flex items-start gap-2 rounded-md border px-3 py-2 text-xs ${
                errorMessage.tone === "warn"
                  ? "border-warn/40 bg-warn/5 text-warn"
                  : "border-destructive/40 bg-destructive/5 text-destructive"
              }`}
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{errorMessage.text}</span>
            </div>
          ) : null}

          <div className="flex justify-end gap-2 pt-2">
            <DialogClose asChild>
              <Button type="button" variant="ghost" size="sm">
                <X className="mr-1 h-3.5 w-3.5" />
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="submit"
              size="sm"
              disabled={mutation.isPending || !form.model_name || !form.input_path}
              className="gap-2"
            >
              {mutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {mutation.isPending ? "Submitting…" : "Start training"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function bodyToForm(body?: Partial<TrainModelBody>): FormState {
  return {
    model_name: body?.model_name ?? DEFAULTS.model_name,
    input_path: body?.input_path ?? DEFAULTS.input_path,
    horizon_bars: String(body?.horizon_bars ?? DEFAULTS.horizon_bars),
    bar_seconds: String(body?.bar_seconds ?? DEFAULTS.bar_seconds),
    cv_folds: String(body?.cv_folds ?? DEFAULTS.cv_folds),
    purge_bars: String(body?.purge_bars ?? DEFAULTS.purge_bars),
    embargo_bars: String(body?.embargo_bars ?? DEFAULTS.embargo_bars),
    num_boost_round: String(
      body?.num_boost_round ?? DEFAULTS.num_boost_round,
    ),
    early_stopping_rounds: String(
      body?.early_stopping_rounds ?? DEFAULTS.early_stopping_rounds,
    ),
  };
}

function suggestedModelName(prefix: string): string {
  const stamp = new Date()
    .toISOString()
    .slice(0, 16)
    .replace(/[-:T]/g, "");
  return `${prefix}_${stamp}`;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-[11px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      {children}
      {hint ? (
        <span className="mt-0.5 block text-[10px] text-muted-foreground">
          {hint}
        </span>
      ) : null}
    </label>
  );
}
