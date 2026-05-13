"use client";

/**
 * SymbolsInput — chip-style multi-select with live ticker typeahead.
 *
 * Behaviour:
 *
 *   - As the user types, debounce 150ms then call
 *     ``GET /data/symbols/search?q=...`` and render the top matches in
 *     a dropdown below the input.
 *   - Arrow Up / Down to navigate the dropdown, Enter to commit the
 *     highlighted suggestion, Escape to close.  Tab and Comma commit
 *     the currently-typed text as-is (operator override for tickers
 *     that aren't in the catalog yet).
 *   - Backspace on an empty input removes the trailing chip.
 *   - Paste a comma-separated list and every symbol commits at once.
 *   - When the dropdown is closed and the user presses Enter, the
 *     literal draft text commits — same behaviour as before this
 *     component had typeahead.
 *
 * Symbol case
 * ~~~~~~~~~~~
 *
 * Suggestions returned by the search endpoint are already canonical
 * upper-case (NVDA, BTC-USD).  Free-typed symbols are preserved
 * verbatim so an operator can name a custom universe row in any case
 * they like and have it round-trip exactly.
 */

import { useQuery } from "@tanstack/react-query";
import { Loader2, Plus, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SymbolMatch } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  /** Show typeahead dropdown; default on. */
  suggestions?: boolean;
}

export function SymbolsInput({
  value,
  onChange,
  placeholder = "Type a symbol — NVDA, AAPL, BTC-USD…",
  suggestions = true,
}: Props) {
  const token = useAuth((s) => s.token);
  const [draft, setDraft] = useState("");
  const [debounced, setDebounced] = useState("");
  const [highlight, setHighlight] = useState(0);
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Debounce the query 150ms — 100-200ms is the sweet spot for
  // typeahead: fast enough to feel live, slow enough that a typist
  // doesn't fire a request per keystroke.
  useEffect(() => {
    if (!draft.trim()) {
      setDebounced("");
      return;
    }
    const t = setTimeout(() => setDebounced(draft.trim()), 150);
    return () => clearTimeout(t);
  }, [draft]);

  const search = useQuery({
    queryKey: ["data", "symbols", "search", debounced],
    queryFn: () =>
      api.searchSymbols(token, debounced, { limit: 10 }),
    enabled: !!token && suggestions && debounced.length > 0,
    staleTime: 30_000,
    placeholderData: (prev) => prev, // keep the previous list visible while refetching
  });

  // Filter out symbols already chosen so the dropdown never offers a
  // duplicate.  Memoised because we recompute on every render.
  const hasSet = useMemo(() => new Set(value), [value]);
  const matches: SymbolMatch[] = useMemo(
    () =>
      (search.data ?? []).filter((m) => !hasSet.has(m.symbol)),
    [search.data, hasSet],
  );

  // Keep the highlight index in range as matches change.
  useEffect(() => {
    if (highlight >= matches.length) setHighlight(0);
  }, [matches.length, highlight]);

  // Click-outside to close the dropdown.  We only listen when open so
  // the listener cost is paid only during interaction.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (event: MouseEvent) => {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const commitText = (raw: string) => {
    const parts = raw
      .split(/[,\s]+/)
      .map((p) => p.trim())
      .filter(Boolean);
    if (parts.length === 0) return;
    const next = Array.from(new Set([...value, ...parts]));
    onChange(next);
    setDraft("");
    setDebounced("");
    setHighlight(0);
  };

  const commitMatch = (match: SymbolMatch) => {
    if (hasSet.has(match.symbol)) return;
    onChange([...value, match.symbol]);
    setDraft("");
    setDebounced("");
    setHighlight(0);
    // Refocus so the operator can immediately type the next symbol.
    inputRef.current?.focus();
  };

  const remove = (symbol: string) => {
    onChange(value.filter((s) => s !== symbol));
  };

  const dropdownVisible =
    suggestions && open && draft.trim().length > 0 && (matches.length > 0 || search.isFetching);

  return (
    <div ref={wrapperRef} className="relative space-y-1.5">
      <div
        className={cn(
          "flex min-h-[38px] flex-wrap items-center gap-1.5 border border-input bg-background p-1.5 transition-colors",
          "focus-within:border-primary/60 focus-within:ring-1 focus-within:ring-primary/40",
        )}
      >
        {value.map((s) => (
          <span
            key={s}
            className="inline-flex items-center gap-1 border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-primary"
          >
            {s}
            <button
              type="button"
              onClick={() => remove(s)}
              aria-label={`Remove ${s}`}
              className="text-primary/60 hover:text-destructive"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          type="text"
          value={draft}
          autoComplete="off"
          spellCheck={false}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setDraft(e.target.value);
            setOpen(true);
          }}
          onKeyDown={(e) => {
            // Dropdown nav takes priority when there are matches showing.
            if (dropdownVisible && matches.length > 0) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setHighlight((h) => (h + 1) % matches.length);
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setHighlight((h) => (h - 1 + matches.length) % matches.length);
                return;
              }
              if (e.key === "Enter") {
                e.preventDefault();
                commitMatch(matches[highlight]);
                return;
              }
            }
            if (e.key === "Escape") {
              setOpen(false);
              return;
            }
            if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
              if (draft.trim()) {
                e.preventDefault();
                commitText(draft);
              }
              return;
            }
            if (e.key === "Backspace" && draft === "" && value.length > 0) {
              e.preventDefault();
              onChange(value.slice(0, -1));
            }
          }}
          onBlur={() => {
            // Defer so a click on a dropdown row gets to commit before
            // the blur drops the draft.  150ms matches the click delay
            // of the dropdown's onMouseDown.
            setTimeout(() => {
              if (draft.trim()) commitText(draft);
            }, 150);
          }}
          onPaste={(e) => {
            const text = e.clipboardData.getData("text");
            if (/[,\s]/.test(text)) {
              e.preventDefault();
              commitText(text);
            }
          }}
          placeholder={value.length === 0 ? placeholder : ""}
          className="min-w-[10rem] flex-1 bg-transparent px-1 text-sm outline-none placeholder:text-muted-foreground"
        />
      </div>

      {dropdownVisible ? (
        <div
          role="listbox"
          aria-label="Symbol suggestions"
          className={cn(
            "absolute left-0 right-0 top-full z-20 mt-1 max-h-72 overflow-auto",
            "border border-border bg-popover text-popover-foreground shadow-lg",
          )}
        >
          {search.isFetching && matches.length === 0 ? (
            <div className="flex items-center gap-2 px-3 py-2 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Searching for “{draft.trim()}”…
            </div>
          ) : matches.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-muted-foreground">
              No matches.{" "}
              <span className="text-foreground/80">Press Enter</span> to add as-is.
            </div>
          ) : (
            matches.map((m, i) => (
              <button
                key={m.symbol}
                type="button"
                role="option"
                aria-selected={i === highlight}
                onMouseDown={(e) => {
                  // mousedown (not click) fires before the input's blur,
                  // so we don't have to fight the blur-commit timer.
                  e.preventDefault();
                  commitMatch(m);
                }}
                onMouseEnter={() => setHighlight(i)}
                className={cn(
                  "flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left transition-colors",
                  i === highlight
                    ? "bg-primary/10 text-foreground"
                    : "text-foreground/90 hover:bg-accent/50",
                )}
              >
                <span className="flex min-w-0 flex-1 items-baseline gap-2">
                  <span className="font-mono text-[12px] font-semibold">
                    {m.symbol}
                  </span>
                  <span className="truncate text-[11px] text-muted-foreground">
                    {m.name}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <span className="font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
                    {m.asset_class.replace("_spot", "")}
                  </span>
                  {m.source === "universe" ? (
                    <span
                      title="Configured in your universe table"
                      className="border border-long/40 bg-long/10 px-1 font-mono text-[9px] uppercase tracking-widest text-long"
                    >
                      universe
                    </span>
                  ) : null}
                </span>
              </button>
            ))
          )}
        </div>
      ) : null}

      {/* Keyboard-shortcut hint, shown only when nothing is typed yet
          so the row collapses when the dropdown takes over. */}
      {suggestions && draft === "" ? (
        <p className="text-[10px] text-muted-foreground/80">
          Type a ticker · {" "}
          <kbd className="rounded border border-border/60 bg-background/60 px-1 font-mono text-[10px]">
            ↑↓
          </kbd>{" "}
          navigate ·{" "}
          <kbd className="rounded border border-border/60 bg-background/60 px-1 font-mono text-[10px]">
            Enter
          </kbd>{" "}
          add ·{" "}
          <kbd className="rounded border border-border/60 bg-background/60 px-1 font-mono text-[10px]">
            ⌫
          </kbd>{" "}
          remove last
        </p>
      ) : null}

      {/* Quick-pick row only when the input is empty and we already
          have a universe loaded (cheap, sourced from React Query
          cache).  Keeps muscle memory for the "I just want my usual
          symbols" path. */}
      {suggestions && draft === "" ? (
        <QuickPicks excluded={hasSet} onPick={(sym) => commitText(sym)} />
      ) : null}
    </div>
  );
}

/**
 * Tiny "your universe" pill row, shown only when the operator hasn't
 * started typing yet.  Sources from the universe endpoint (already
 * cached in React Query by the rest of the app), so this is free.
 */
function QuickPicks({
  excluded,
  onPick,
}: {
  excluded: Set<string>;
  onPick: (symbol: string) => void;
}) {
  const token = useAuth((s) => s.token);
  const universe = useQuery({
    queryKey: ["data", "universe"],
    queryFn: () => api.universe(token),
    enabled: !!token,
    staleTime: 60_000,
  });
  const available = (universe.data ?? [])
    .map((u) => u.symbol)
    .filter((s) => !excluded.has(s));
  if (available.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        From your universe:
      </span>
      {available.slice(0, 8).map((s) => (
        <button
          key={s}
          type="button"
          onClick={() => onPick(s)}
          className={cn(
            "group inline-flex items-center gap-1 border border-dashed border-border/60 bg-background/30 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground transition-colors",
            "hover:border-primary/60 hover:bg-primary/5 hover:text-primary",
          )}
        >
          <Plus className="h-2.5 w-2.5 opacity-60 group-hover:opacity-100" />
          {s}
        </button>
      ))}
      {available.length > 8 ? (
        <span className="text-[10px] text-muted-foreground/50">
          +{available.length - 8} more
        </span>
      ) : null}
    </div>
  );
}
