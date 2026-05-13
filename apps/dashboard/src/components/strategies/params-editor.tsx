"use client";

/**
 * ParamsEditor — quick-presets + dual-mode editor for ``params``.
 *
 * Layout (top → bottom):
 *
 *   1. Quick-start preset row    one-click bundles per class_name
 *                                ("Default", "Conservative",
 *                                "Aggressive", "Sandbox").  Hidden
 *                                when no presets exist for the class.
 *   2. Form / JSON mode toggle   power-user fallback for tweaks.
 *   3. The active editor pane.
 *
 * Two editing modes share one source of truth:
 *
 *   - "Form" mode: one row per key/value pair, keyboard-addable.
 *     Values are parsed at commit time (numbers stay numbers,
 *     booleans stay booleans, anything else is a string).
 *   - "JSON" mode: raw JSON textarea for power users pasting a
 *     larger block.  Parsed on every keystroke with inline error
 *     reporting; the form mode stays out of sync until JSON is
 *     valid, at which point the form snaps to the new structure.
 *
 * Why not a typed schema per class_name?
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 *
 * Each strategy class has different required params; encoding them
 * here would put the dashboard ahead of the api on adding new
 * classes.  Until the strategy host exposes a JSON schema per class,
 * this flexible editor is the correct trade-off: the operator types
 * what the class needs, the api/host validates on write.
 */

import { Braces, List, Plus, Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  type ParamsPreset,
  type PresetTone,
  getPresets,
} from "@/components/strategies/params-presets";
import { cn } from "@/lib/utils";

interface Row {
  key: string;
  value: string;
}

export function ParamsEditor({
  value,
  onChange,
  classKey = null,
}: {
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** Strategy class_name; drives which preset buttons appear.  Pass
   * null when the editor isn't inside a class-aware form. */
  classKey?: string | null;
}) {
  const presets = useMemo(() => getPresets(classKey), [classKey]);
  const [mode, setMode] = useState<"form" | "json">("form");
  const [rows, setRows] = useState<Row[]>(() => toRows(value));
  const [jsonText, setJsonText] = useState(() => toJson(value));
  const [jsonError, setJsonError] = useState<string | null>(null);

  // If the parent-controlled value changes externally (e.g. the
  // dialog reset on open), snap both modes to the new source.
  useEffect(() => {
    setRows(toRows(value));
    setJsonText(toJson(value));
    setJsonError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(value)]);

  const emit = (rowsNext: Row[]) => {
    const obj: Record<string, unknown> = {};
    for (const r of rowsNext) {
      const k = r.key.trim();
      if (!k) continue;
      obj[k] = parseScalar(r.value);
    }
    onChange(obj);
    setJsonText(JSON.stringify(obj, null, 2));
  };

  const updateRow = (idx: number, patch: Partial<Row>) => {
    const next = rows.map((r, i) => (i === idx ? { ...r, ...patch } : r));
    setRows(next);
    emit(next);
  };
  const addRow = () => {
    const next = [...rows, { key: "", value: "" }];
    setRows(next);
  };
  const removeRow = (idx: number) => {
    const next = rows.filter((_, i) => i !== idx);
    setRows(next);
    emit(next);
  };

  const handleJsonChange = (text: string) => {
    setJsonText(text);
    if (!text.trim()) {
      setJsonError(null);
      onChange({});
      setRows([]);
      return;
    }
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setJsonError("must be a JSON object");
        return;
      }
      setJsonError(null);
      onChange(parsed as Record<string, unknown>);
      setRows(toRows(parsed as Record<string, unknown>));
    } catch (e) {
      setJsonError(e instanceof Error ? e.message : String(e));
    }
  };

  const rowCount = useMemo(
    () => rows.filter((r) => r.key.trim()).length,
    [rows],
  );

  const applyPreset = (preset: ParamsPreset) => {
    // Replace, don't merge: the presets are intentionally complete
    // for their use case, and merging on top of stale params is the
    // single most confusing behaviour we could ship.
    onChange({ ...preset.params });
  };

  return (
    <div className="space-y-2">
      {presets.length > 0 ? (
        <PresetRow
          presets={presets}
          current={value}
          onApply={applyPreset}
        />
      ) : null}

      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1">
          <ModeButton
            active={mode === "form"}
            icon={List}
            label="Form"
            onClick={() => setMode("form")}
          />
          <ModeButton
            active={mode === "json"}
            icon={Braces}
            label="JSON"
            onClick={() => setMode("json")}
          />
        </div>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {rowCount} param{rowCount === 1 ? "" : "s"}
        </span>
      </div>

      {mode === "form" ? (
        <div className="space-y-1.5">
          {rows.length === 0 ? (
            <div className="flex items-center justify-between border border-dashed border-border/60 bg-background/30 px-3 py-2 text-[11px] text-muted-foreground">
              <span>No params — the strategy will use defaults.</span>
              <button
                type="button"
                onClick={addRow}
                className="inline-flex items-center gap-1 text-primary hover:text-primary/80"
              >
                <Plus className="h-3 w-3" />
                Add
              </button>
            </div>
          ) : (
            rows.map((r, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <input
                  type="text"
                  value={r.key}
                  onChange={(e) => updateRow(i, { key: e.target.value })}
                  placeholder="key"
                  className="h-8 w-1/3 border border-input bg-background px-2 font-mono text-xs outline-none focus:border-primary/60"
                />
                <span className="text-muted-foreground">=</span>
                <input
                  type="text"
                  value={r.value}
                  onChange={(e) => updateRow(i, { value: e.target.value })}
                  placeholder="value"
                  className="h-8 flex-1 border border-input bg-background px-2 font-mono text-xs outline-none focus:border-primary/60"
                />
                <button
                  type="button"
                  onClick={() => removeRow(i)}
                  aria-label={`Remove ${r.key || "row"}`}
                  className="flex h-8 w-8 items-center justify-center border border-border/60 bg-background/30 text-muted-foreground transition-colors hover:border-destructive/60 hover:text-destructive"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ))
          )}
          {rows.length > 0 ? (
            <button
              type="button"
              onClick={addRow}
              className="inline-flex items-center gap-1 border border-dashed border-border/60 bg-background/30 px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground transition-colors hover:border-primary/60 hover:text-primary"
            >
              <Plus className="h-3 w-3" />
              Add param
            </button>
          ) : null}
        </div>
      ) : (
        <div className="space-y-1">
          <textarea
            value={jsonText}
            onChange={(e) => handleJsonChange(e.target.value)}
            spellCheck={false}
            rows={6}
            className={cn(
              "w-full resize-y border border-input bg-background px-2 py-1.5 font-mono text-xs leading-relaxed outline-none focus:border-primary/60",
              jsonError && "border-destructive/60 focus:border-destructive",
            )}
            placeholder='{"fast": 5, "slow": 20}'
          />
          {jsonError ? (
            <p className="text-[11px] text-destructive">JSON: {jsonError}</p>
          ) : (
            <p className="text-[10px] text-muted-foreground">
              Raw JSON object — parsed on every keystroke.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Preset row                                                                  //
// --------------------------------------------------------------------------- //

function PresetRow({
  presets,
  current,
  onApply,
}: {
  presets: ParamsPreset[];
  current: Record<string, unknown>;
  onApply: (preset: ParamsPreset) => void;
}) {
  // A preset is "active" iff every one of its keys is already present
  // in current AND has the same value (deep-equal via JSON).  This
  // lets the operator see at a glance which preset they're on after
  // a click — and it auto-clears as soon as they edit anything.
  const currentJson = JSON.stringify(current);
  const isActive = (preset: ParamsPreset) => {
    for (const [k, v] of Object.entries(preset.params)) {
      if (JSON.stringify(current[k]) !== JSON.stringify(v)) return false;
    }
    return Object.keys(preset.params).length === Object.keys(current).length;
  };

  return (
    <div className="border border-cyan/20 bg-cyan/[0.02] p-2">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-widest text-cyan">
          <Sparkles className="h-3 w-3" />
          Quick presets
        </span>
        <span className="text-[10px] text-muted-foreground">
          one-click setup · tweak below
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {presets.map((preset) => {
          const active = isActive(preset);
          return (
            <button
              key={preset.label}
              type="button"
              onClick={() => onApply(preset)}
              title={preset.description}
              className={cn(
                "group inline-flex flex-col items-start gap-0.5 border px-2 py-1 text-left transition-all",
                active
                  ? activeToneClass(preset.tone)
                  : idleToneClass(preset.tone),
              )}
            >
              <span className="font-mono text-[11px] font-semibold">
                {preset.label}
              </span>
              <span
                className={cn(
                  "max-w-[14rem] truncate text-[9px] leading-tight",
                  active
                    ? "text-foreground/70"
                    : "text-muted-foreground group-hover:text-foreground/80",
                )}
              >
                {preset.description}
              </span>
            </button>
          );
        })}
      </div>
      {/* Render once we've seen the current params — silences a
          would-be unused-var warning while keeping the JSON in scope
          for readers who wonder where active comparisons happen. */}
      <span className="hidden" data-current-hash={currentJson} />
    </div>
  );
}

function activeToneClass(tone: PresetTone): string {
  switch (tone) {
    case "long":
      return "border-long/60 bg-long/10 text-long shadow-[inset_0_0_0_1px_hsl(var(--long)/0.4)]";
    case "short":
      return "border-short/60 bg-short/10 text-short shadow-[inset_0_0_0_1px_hsl(var(--short)/0.4)]";
    case "muted":
      return "border-muted-foreground/40 bg-muted/30 text-foreground shadow-[inset_0_0_0_1px_hsl(var(--muted-foreground)/0.3)]";
    case "primary":
    default:
      return "border-primary/60 bg-primary/10 text-primary shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.4)]";
  }
}

function idleToneClass(tone: PresetTone): string {
  switch (tone) {
    case "long":
      return "border-border/60 bg-background/30 text-muted-foreground hover:border-long/50 hover:bg-long/5 hover:text-long";
    case "short":
      return "border-border/60 bg-background/30 text-muted-foreground hover:border-short/50 hover:bg-short/5 hover:text-short";
    case "muted":
      return "border-border/60 bg-background/30 text-muted-foreground hover:border-border hover:bg-accent/30 hover:text-foreground";
    case "primary":
    default:
      return "border-border/60 bg-background/30 text-muted-foreground hover:border-primary/50 hover:bg-primary/5 hover:text-primary";
  }
}

// --------------------------------------------------------------------------- //
// Mode toggle                                                                 //
// --------------------------------------------------------------------------- //

function ModeButton({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 border px-2 py-1 text-[10px] uppercase tracking-widest transition-colors",
        active
          ? "border-primary/60 bg-primary/5 text-primary"
          : "border-border/60 bg-background/30 text-muted-foreground hover:border-primary/40 hover:text-foreground",
      )}
    >
      <Icon className="h-3 w-3" />
      {label}
    </button>
  );
}

function toRows(obj: Record<string, unknown>): Row[] {
  return Object.entries(obj).map(([k, v]) => ({
    key: k,
    value: scalarToString(v),
  }));
}

function toJson(obj: Record<string, unknown>): string {
  if (Object.keys(obj).length === 0) return "";
  return JSON.stringify(obj, null, 2);
}

function scalarToString(v: unknown): string {
  if (v === null) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Best-effort scalar parse: numbers stay numbers, true/false stay bool,
 * JSON-looking objects/arrays parse, otherwise string. */
function parseScalar(raw: string): unknown {
  const t = raw.trim();
  if (t === "") return "";
  if (t === "null") return null;
  if (t === "true") return true;
  if (t === "false") return false;
  // numeric?
  if (/^-?\d+(\.\d+)?$/.test(t)) {
    const n = Number(t);
    if (Number.isFinite(n)) return n;
  }
  // JSON-looking?
  if (
    (t.startsWith("{") && t.endsWith("}")) ||
    (t.startsWith("[") && t.endsWith("]"))
  ) {
    try {
      return JSON.parse(t);
    } catch {
      // fall through
    }
  }
  return raw;
}
