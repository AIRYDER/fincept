"use client";

/**
 * SymbolsPills — monospace chips of symbols the strategy trades.
 *
 * Overflow is bounded: the first N are rendered verbatim, any excess
 * collapses to a ``+K more`` pill with a title-attribute revealing
 * the full list.  This keeps row heights stable on the list view
 * even for baskets of 30+ symbols.
 */

import { cn } from "@/lib/utils";

export function SymbolsPills({
  symbols,
  max = 4,
  className,
}: {
  symbols: string[];
  max?: number;
  className?: string;
}) {
  if (symbols.length === 0) {
    return (
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        No symbols
      </span>
    );
  }
  const shown = symbols.slice(0, max);
  const hidden = symbols.slice(max);
  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)}>
      {shown.map((s) => (
        <span
          key={s}
          className="inline-flex items-center border border-border/60 bg-background/40 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-foreground/90"
        >
          {s}
        </span>
      ))}
      {hidden.length > 0 ? (
        <span
          title={hidden.join(", ")}
          className="inline-flex items-center border border-dashed border-border/60 bg-background/30 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground"
        >
          +{hidden.length}
        </span>
      ) : null}
    </div>
  );
}
