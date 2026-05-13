"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Plus,
  Send,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

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
import type {
  OrderType,
  PlaceOrderBody,
  Position,
  Side,
  SymbolMatch,
  TimeInForce,
} from "@/lib/types";
import { cn, formatNumber, formatUsd } from "@/lib/utils";

type Draft = {
  symbol: string;
  side: Side;
  order_type: OrderType;
  quantity: string;
  limit_price: string;
  stop_price: string;
  time_in_force: TimeInForce;
  strategy_id: string;
};

const DEFAULT_DRAFT: Draft = {
  symbol: "",
  side: "buy",
  order_type: "market",
  quantity: "1",
  limit_price: "",
  stop_price: "",
  time_in_force: "day",
  strategy_id: "manual",
};

const ORDER_TYPES: Array<{ value: OrderType; label: string; hint: string }> = [
  { value: "market", label: "Market", hint: "Immediate execution" },
  { value: "limit", label: "Limit", hint: "Cap entry or exit price" },
  { value: "stop", label: "Stop", hint: "Trigger on stop price" },
  { value: "stop_limit", label: "Stop limit", hint: "Trigger, then limit" },
];

const QUICK_TICKETS: Array<{
  label: string;
  description: string;
  patch: Partial<Draft>;
}> = [
  {
    label: "Test buy",
    description: "1 share market, day",
    patch: {
      side: "buy",
      order_type: "market",
      quantity: "1",
      time_in_force: "day",
      limit_price: "",
      stop_price: "",
    },
  },
  {
    label: "Scale in",
    description: "10 shares limit",
    patch: {
      side: "buy",
      order_type: "limit",
      quantity: "10",
      time_in_force: "day",
      stop_price: "",
    },
  },
  {
    label: "Scale out",
    description: "10 shares limit sell",
    patch: {
      side: "sell",
      order_type: "limit",
      quantity: "10",
      time_in_force: "day",
      stop_price: "",
    },
  },
  {
    label: "Protective stop",
    description: "Stop sell template",
    patch: {
      side: "sell",
      order_type: "stop",
      quantity: "1",
      time_in_force: "gtc",
      limit_price: "",
    },
  },
];

export function PlaceOrderDialog({ trigger }: { trigger?: React.ReactNode }) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(DEFAULT_DRAFT);
  const [submittedId, setSubmittedId] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setSubmittedId(null);
  }, [open]);

  const positions = useQuery({
    queryKey: ["positions", true],
    queryFn: () => api.positions(token, true),
    enabled: !!token && open,
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: (body: PlaceOrderBody) => api.placeOrder(token, body),
    onSuccess: (res) => {
      setSubmittedId(res.order_id);
      queryClient.invalidateQueries({ queryKey: ["orders"] });
      queryClient.invalidateQueries({ queryKey: ["positions"] });
    },
  });

  const selectedPosition = useMemo(
    () =>
      (positions.data ?? []).find(
        (p) => p.symbol.toUpperCase() === draft.symbol.trim().toUpperCase(),
      ),
    [positions.data, draft.symbol],
  );

  const formError = validateDraft(draft);
  const errorMessage = apiErrorMessage(mutation.error);
  const estimatedNotional = estimateNotional(draft, selectedPosition);

  const submit = () => {
    const body = toBody(draft);
    if (!body) return;
    mutation.mutate(body);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" className="gap-2">
            <Send className="h-3.5 w-3.5" />
            Place order
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Send className="h-4 w-4 text-primary" />
            Place manual order
          </DialogTitle>
          <DialogDescription>
            Manual orders enter the same OMS stream as strategies, so risk checks,
            audit records, fills, and positions stay in one path.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
          <section className="space-y-4">
            <div className="space-y-2">
              <Label text="Ticker" />
              <SingleSymbolPicker
                value={draft.symbol}
                onChange={(symbol) =>
                  setDraft((s) => ({ ...s, symbol: symbol.toUpperCase() }))
                }
              />
            </div>

            <div className="grid grid-cols-2 gap-2">
              <Segment
                active={draft.side === "buy"}
                tone="buy"
                title="Buy"
                subtitle="Increase or cover"
                onClick={() => setDraft((s) => ({ ...s, side: "buy" }))}
              />
              <Segment
                active={draft.side === "sell"}
                tone="sell"
                title="Sell"
                subtitle="Reduce or short"
                onClick={() => setDraft((s) => ({ ...s, side: "sell" }))}
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Quantity"
                value={draft.quantity}
                onChange={(quantity) => setDraft((s) => ({ ...s, quantity }))}
                placeholder="1"
                inputMode="decimal"
              />
              <div className="space-y-1.5">
                <Label text="Time in force" />
                <div className="grid grid-cols-4 gap-1">
                  {(["day", "gtc", "ioc", "fok"] as TimeInForce[]).map((tif) => (
                    <button
                      key={tif}
                      type="button"
                      onClick={() => setDraft((s) => ({ ...s, time_in_force: tif }))}
                      className={cn(
                        "border border-border/60 px-2 py-2 font-mono text-[10px] uppercase tracking-wider transition-colors",
                        draft.time_in_force === tif
                          ? "border-primary/60 bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-accent",
                      )}
                    >
                      {tif}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label text="Order type" />
              <div className="grid gap-2 sm:grid-cols-2">
                {ORDER_TYPES.map((type) => (
                  <button
                    key={type.value}
                    type="button"
                    onClick={() =>
                      setDraft((s) => ({
                        ...s,
                        order_type: type.value,
                        limit_price:
                          type.value === "market" || type.value === "stop"
                            ? ""
                            : s.limit_price,
                        stop_price:
                          type.value === "market" || type.value === "limit"
                            ? ""
                            : s.stop_price,
                      }))
                    }
                    className={cn(
                      "border border-border/60 bg-background/40 p-3 text-left transition-colors",
                      draft.order_type === type.value
                        ? "border-primary/60 bg-primary/10"
                        : "hover:bg-accent/50",
                    )}
                  >
                    <div className="text-xs font-semibold uppercase tracking-wider">
                      {type.label}
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      {type.hint}
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              {(draft.order_type === "limit" ||
                draft.order_type === "stop_limit") ? (
                <Field
                  label="Limit price"
                  value={draft.limit_price}
                  onChange={(limit_price) =>
                    setDraft((s) => ({ ...s, limit_price }))
                  }
                  placeholder="187.50"
                  inputMode="decimal"
                />
              ) : null}
              {(draft.order_type === "stop" ||
                draft.order_type === "stop_limit") ? (
                <Field
                  label="Stop price"
                  value={draft.stop_price}
                  onChange={(stop_price) =>
                    setDraft((s) => ({ ...s, stop_price }))
                  }
                  placeholder="180.00"
                  inputMode="decimal"
                />
              ) : null}
              <Field
                label="Strategy attribution"
                value={draft.strategy_id}
                onChange={(strategy_id) =>
                  setDraft((s) => ({ ...s, strategy_id }))
                }
                placeholder="manual"
              />
            </div>
          </section>

          <aside className="space-y-3">
            <div className="border border-border/60 bg-background/35 p-3">
              <div className="mb-2 flex items-center gap-2">
                <Sparkles className="h-3.5 w-3.5 text-primary" />
                <Label text="Quick tickets" />
              </div>
              <div className="grid gap-2">
                {QUICK_TICKETS.map((ticket) => (
                  <button
                    key={ticket.label}
                    type="button"
                    onClick={() =>
                      setDraft((s) => ({
                        ...s,
                        ...ticket.patch,
                      }))
                    }
                    className="border border-border/50 bg-card/40 px-3 py-2 text-left transition-colors hover:border-primary/50 hover:bg-primary/5"
                  >
                    <div className="text-[11px] font-semibold uppercase tracking-wider">
                      {ticket.label}
                    </div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">
                      {ticket.description}
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <PositionShortcuts
              positions={positions.data ?? []}
              onPick={(patch) =>
                setDraft((s) => ({
                  ...s,
                  ...patch,
                  order_type: "market",
                  time_in_force: "day",
                  limit_price: "",
                  stop_price: "",
                }))
              }
            />

            <OrderPreview
              draft={draft}
              position={selectedPosition}
              estimatedNotional={estimatedNotional}
            />
          </aside>
        </div>

        {formError ? (
          <InlineMessage tone="warn" text={formError} />
        ) : errorMessage ? (
          <InlineMessage tone={errorMessage.tone} text={errorMessage.text} />
        ) : submittedId ? (
          <div className="flex items-start gap-2 border border-long/40 bg-long/5 px-3 py-2 text-xs text-long">
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              Order submitted. Tracking id{" "}
              <code className="font-mono">{submittedId.slice(0, 12)}</code>.
            </span>
          </div>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-border/40 pt-3">
          <DialogClose asChild>
            <Button type="button" variant="ghost" size="sm">
              Close
            </Button>
          </DialogClose>
          <Button
            type="button"
            size="sm"
            disabled={!!formError || mutation.isPending}
            onClick={submit}
            className="gap-2"
          >
            {mutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="h-3.5 w-3.5" />
            )}
            {mutation.isPending ? "Submitting..." : "Submit order"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SingleSymbolPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (symbol: string) => void;
}) {
  const token = useAuth((s) => s.token);
  const [draft, setDraft] = useState(value);
  const [debounced, setDebounced] = useState("");
  const [highlight, setHighlight] = useState(0);
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => setDraft(value), [value]);

  useEffect(() => {
    const clean = draft.trim();
    if (!clean) {
      setDebounced("");
      return;
    }
    const t = setTimeout(() => setDebounced(clean), 150);
    return () => clearTimeout(t);
  }, [draft]);

  const search = useQuery({
    queryKey: ["data", "symbols", "search", debounced],
    queryFn: () => api.searchSymbols(token, debounced, { limit: 8 }),
    enabled: !!token && debounced.length > 0,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  const matches = search.data ?? [];
  const dropdownVisible =
    open && draft.trim().length > 0 && (matches.length > 0 || search.isFetching);

  useEffect(() => {
    if (highlight >= matches.length) setHighlight(0);
  }, [highlight, matches.length]);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const commit = (symbol: string) => {
    const clean = symbol.trim().toUpperCase();
    onChange(clean);
    setDraft(clean);
    setOpen(false);
  };

  return (
    <div ref={wrapperRef} className="relative">
      <Input
        value={draft}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setDraft(e.target.value.toUpperCase());
          setOpen(true);
        }}
        onBlur={() => {
          setTimeout(() => {
            if (draft.trim()) commit(draft);
          }, 150);
        }}
        onKeyDown={(e) => {
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
              commit(matches[highlight].symbol);
              return;
            }
          }
          if (e.key === "Escape") setOpen(false);
        }}
        placeholder="Start typing NVDA, AAPL, SPY..."
        autoComplete="off"
        spellCheck={false}
        className="font-mono uppercase"
      />
      {dropdownVisible ? (
        <div className="absolute left-0 right-0 top-full z-30 mt-1 max-h-64 overflow-auto border border-border bg-popover shadow-lg">
          {search.isFetching && matches.length === 0 ? (
            <div className="flex items-center gap-2 px-3 py-2 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Matching {draft.trim()}...
            </div>
          ) : matches.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-muted-foreground">
              No match yet. Press Enter to use typed ticker.
            </div>
          ) : (
            matches.map((m: SymbolMatch, i) => (
              <button
                key={m.symbol}
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                  commit(m.symbol);
                }}
                onMouseEnter={() => setHighlight(i)}
                className={cn(
                  "flex w-full items-center justify-between gap-2 px-3 py-2 text-left transition-colors",
                  i === highlight ? "bg-primary/10" : "hover:bg-accent/50",
                )}
              >
                <span className="min-w-0">
                  <span className="font-mono text-xs font-semibold">
                    {m.symbol}
                  </span>
                  <span className="ml-2 text-[11px] text-muted-foreground">
                    {m.name}
                  </span>
                </span>
                <span className="shrink-0 font-mono text-[9px] uppercase tracking-widest text-muted-foreground">
                  {m.asset_class}
                </span>
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

function PositionShortcuts({
  positions,
  onPick,
}: {
  positions: Position[];
  onPick: (patch: Partial<Draft>) => void;
}) {
  const openPositions = positions.filter((p) => Math.abs(Number(p.quantity)) > 0);
  if (openPositions.length === 0) return null;

  return (
    <div className="border border-border/60 bg-background/35 p-3">
      <Label text="Position actions" />
      <div className="mt-2 grid gap-2">
        {openPositions.slice(0, 4).map((p) => {
          const qty = Number(p.quantity);
          const closeSide: Side = qty > 0 ? "sell" : "buy";
          return (
            <button
              key={`${p.strategy_id}:${p.symbol}`}
              type="button"
              onClick={() =>
                onPick({
                  symbol: p.symbol,
                  side: closeSide,
                  quantity: String(Math.abs(qty)),
                  strategy_id: "manual",
                })
              }
              className="border border-border/50 bg-card/40 px-3 py-2 text-left transition-colors hover:border-primary/50 hover:bg-primary/5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-[11px] font-semibold">
                  Close {p.symbol}
                </span>
                <span
                  className={cn(
                    "font-mono text-[10px]",
                    closeSide === "sell" ? "text-short" : "text-long",
                  )}
                >
                  {closeSide.toUpperCase()} {formatNumber(Math.abs(qty), 6)}
                </span>
              </div>
              <div className="mt-0.5 text-[10px] text-muted-foreground">
                {p.strategy_id} position
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function OrderPreview({
  draft,
  position,
  estimatedNotional,
}: {
  draft: Draft;
  position?: Position;
  estimatedNotional: number | null;
}) {
  return (
    <div className="border border-border/60 bg-card/35 p-3">
      <Label text="Review" />
      <dl className="mt-3 space-y-2 text-xs">
        <PreviewRow label="Symbol" value={draft.symbol || "--"} mono />
        <PreviewRow
          label="Action"
          value={`${draft.side.toUpperCase()} ${draft.quantity || "--"}`}
          tone={draft.side === "buy" ? "buy" : "sell"}
        />
        <PreviewRow label="Type" value={draft.order_type.replace("_", " ")} />
        <PreviewRow label="TIF" value={draft.time_in_force.toUpperCase()} mono />
        <PreviewRow
          label="Est. notional"
          value={estimatedNotional ? formatUsd(estimatedNotional) : "--"}
        />
        <PreviewRow
          label="Current qty"
          value={position ? formatNumber(position.quantity, 6) : "--"}
          mono
        />
      </dl>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  inputMode,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  inputMode?: React.HTMLAttributes<HTMLInputElement>["inputMode"];
}) {
  return (
    <label className="block space-y-1.5">
      <Label text={label} />
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        inputMode={inputMode}
      />
    </label>
  );
}

function Segment({
  active,
  tone,
  title,
  subtitle,
  onClick,
}: {
  active: boolean;
  tone: "buy" | "sell";
  title: string;
  subtitle: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "border p-3 text-left transition-colors",
        active
          ? tone === "buy"
            ? "border-long/60 bg-long/10 text-long"
            : "border-short/60 bg-short/10 text-short"
          : "border-border/60 bg-background/35 hover:bg-accent/50",
      )}
    >
      <div className="text-xs font-semibold uppercase tracking-wider">{title}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{subtitle}</div>
    </button>
  );
}

function Label({ text }: { text: string }) {
  return (
    <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
      {text}
    </span>
  );
}

function PreviewRow({
  label,
  value,
  mono,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: "buy" | "sell";
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "text-right font-medium",
          mono && "font-mono",
          tone === "buy" && "text-long",
          tone === "sell" && "text-short",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function InlineMessage({
  tone,
  text,
}: {
  tone: "warn" | "danger";
  text: string;
}) {
  return (
    <div
      className={cn(
        "flex items-start gap-2 border px-3 py-2 text-xs",
        tone === "warn"
          ? "border-warning/40 bg-warning/5 text-warning"
          : "border-destructive/40 bg-destructive/5 text-destructive",
      )}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{text}</span>
    </div>
  );
}

function validateDraft(draft: Draft): string | null {
  if (!draft.symbol.trim()) return "Choose a ticker before submitting.";
  if (!isPositiveDecimal(draft.quantity)) return "Quantity must be positive.";
  if (
    (draft.order_type === "limit" || draft.order_type === "stop_limit") &&
    !isPositiveDecimal(draft.limit_price)
  ) {
    return "Limit orders require a positive limit price.";
  }
  if (
    (draft.order_type === "stop" || draft.order_type === "stop_limit") &&
    !isPositiveDecimal(draft.stop_price)
  ) {
    return "Stop orders require a positive stop price.";
  }
  if (!draft.strategy_id.trim()) return "Strategy attribution is required.";
  return null;
}

function toBody(draft: Draft): PlaceOrderBody | null {
  if (validateDraft(draft)) return null;
  return {
    symbol: draft.symbol.trim().toUpperCase(),
    side: draft.side,
    order_type: draft.order_type,
    quantity: draft.quantity.trim(),
    limit_price:
      draft.order_type === "limit" || draft.order_type === "stop_limit"
        ? draft.limit_price.trim()
        : null,
    stop_price:
      draft.order_type === "stop" || draft.order_type === "stop_limit"
        ? draft.stop_price.trim()
        : null,
    time_in_force: draft.time_in_force,
    venue: "alpaca",
    strategy_id: draft.strategy_id.trim(),
    tags: { source_ui: "orders.place_order_dialog" },
  };
}

function isPositiveDecimal(value: string): boolean {
  const n = Number(value);
  return Number.isFinite(n) && n > 0;
}

function estimateNotional(draft: Draft, position?: Position): number | null {
  const qty = Number(draft.quantity);
  if (!Number.isFinite(qty) || qty <= 0) return null;
  const explicit =
    draft.order_type === "limit" || draft.order_type === "stop_limit"
      ? Number(draft.limit_price)
      : draft.order_type === "stop"
        ? Number(draft.stop_price)
        : Number(position?.mark_px ?? position?.avg_cost);
  if (!Number.isFinite(explicit) || explicit <= 0) return null;
  return qty * explicit;
}
