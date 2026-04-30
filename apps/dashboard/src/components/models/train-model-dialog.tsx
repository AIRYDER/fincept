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

interface FormState {
  model_name: string;
  input_path: string;
  horizon_bars: string;
  bar_seconds: string;
  cv_folds: string;
  num_boost_round: string;
  early_stopping_rounds: string;
}

const DEFAULTS: FormState = {
  model_name: "",
  input_path: "",
  horizon_bars: "15",
  bar_seconds: "60",
  cv_folds: "5",
  num_boost_round: "500",
  early_stopping_rounds: "30",
};

export function TrainModelDialog() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(DEFAULTS);

  const mutation = useMutation({
    mutationFn: (body: TrainModelBody) => api.trainModel(token, body),
    onSuccess: () => {
      // The runs list polls but we kick a refetch immediately so the
      // user sees their submission within the same render.
      queryClient.invalidateQueries({ queryKey: ["models", "runs"] });
      queryClient.invalidateQueries({ queryKey: ["models"] });
      // Reset and close.
      setForm(DEFAULTS);
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
      num_boost_round: Number.parseInt(form.num_boost_round, 10),
      early_stopping_rounds: Number.parseInt(form.early_stopping_rounds, 10),
    };
    mutation.mutate(body);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="gap-2">
          <Play className="h-3.5 w-3.5" />
          Train new model
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
              placeholder="data/bars_with_features.parquet"
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
