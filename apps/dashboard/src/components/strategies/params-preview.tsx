"use client";

/**
 * ParamsPreview — compact key=value rendering of a strategy's params
 * dict.  Empty dicts render an explicit ``default`` pill rather than
 * nothing, so the operator can distinguish "no params set" from "I
 * forgot to fetch the config".
 *
 * The full value is always available via title-attribute hover for
 * copy-paste debugging.
 */

import { cn } from "@/lib/utils";

export function ParamsPreview({
  params,
  max = 3,
  className,
}: {
  params: Record<string, unknown>;
  max?: number;
  className?: string;
}) {
  const entries = Object.entries(params);
  if (entries.length === 0) {
    return (
      <span
        className={cn(
          "inline-flex items-center border border-dashed border-border/60 bg-background/30 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground",
          className,
        )}
      >
        default params
      </span>
    );
  }
  const shown = entries.slice(0, max);
  const hidden = entries.slice(max);
  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)}>
      {shown.map(([k, v]) => (
        <span
          key={k}
          title={`${k} = ${stringifyValue(v)}`}
          className="inline-flex max-w-[14rem] items-center border border-border/60 bg-background/40 px-1.5 py-0.5 font-mono text-[10px] text-foreground/80"
        >
          <span className="truncate text-muted-foreground">{k}</span>
          <span className="mx-1 text-muted-foreground/60">=</span>
          <span className="truncate">{stringifyValue(v)}</span>
        </span>
      ))}
      {hidden.length > 0 ? (
        <span
          title={hidden.map(([k, v]) => `${k} = ${stringifyValue(v)}`).join("\n")}
          className="inline-flex items-center border border-dashed border-border/60 bg-background/30 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
        >
          +{hidden.length}
        </span>
      ) : null}
    </div>
  );
}

function stringifyValue(v: unknown): string {
  if (v === null) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
