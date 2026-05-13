"use client";

import { BrainCircuit, ChevronDown, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn, formatUsd } from "@/lib/utils";

import { defaultPortfolioPreferences } from "./portfolioOptimizer";
import type {
  PortfolioBuilderInput,
  PortfolioModelProvider,
  PortfolioRiskLevel,
  PortfolioSector,
  PortfolioTimeHorizon,
  RebalanceFrequency,
} from "./portfolioBuilder.types";

const HORIZONS: Array<{ value: PortfolioTimeHorizon; label: string; detail: string }> = [
  { value: "3m", label: "3M", detail: "capital preservation" },
  { value: "6m", label: "6M", detail: "near-term setup" },
  { value: "1y", label: "1Y", detail: "cycle-aware" },
  { value: "3y", label: "3Y", detail: "base case" },
  { value: "5y", label: "5Y", detail: "compounders" },
  { value: "10y_plus", label: "10Y+", detail: "structural" },
  { value: "custom", label: "Custom", detail: "describe below" },
];

const RISKS: Array<{ value: PortfolioRiskLevel; label: string; detail: string }> = [
  { value: "conservative", label: "Conservative", detail: "defense first" },
  { value: "balanced", label: "Balanced", detail: "core plus upside" },
  { value: "growth", label: "Growth", detail: "quality growth" },
  { value: "aggressive_growth", label: "Aggressive", detail: "thematic upside" },
  { value: "speculative", label: "Speculative", detail: "high dispersion" },
];

const SECTOR_GROUPS: Array<{
  label: string;
  description: string;
  items: Array<{ value: PortfolioSector; label: string }>;
}> = [
  {
    label: "Core allocation",
    description: "Whole-market ballast and cash-like reserves.",
    items: [
      { value: "broad_etfs", label: "Broad ETFs" },
      { value: "cash_treasuries", label: "T-Bills / Cash" },
    ],
  },
  {
    label: "AI compute stack",
    description: "Chips, AI infrastructure, cloud, and security rails.",
    items: [
      { value: "semiconductors", label: "Semiconductors" },
      { value: "ai_infrastructure", label: "AI Infrastructure" },
      { value: "cloud_computing", label: "Cloud Platforms" },
      { value: "cybersecurity", label: "Cybersecurity" },
    ],
  },
  {
    label: "Power and energy",
    description: "Grid load, nuclear fuel, oil cash flow, and clean power.",
    items: [
      { value: "energy", label: "Energy Basket" },
      { value: "nuclear_energy", label: "Nuclear Power" },
      { value: "uranium", label: "Uranium" },
      { value: "oil_gas", label: "Oil & Gas" },
      { value: "renewables", label: "Renewables" },
    ],
  },
  {
    label: "Sovereign infrastructure",
    description: "Defense, aerospace, and real-economy industrial capacity.",
    items: [
      { value: "defense", label: "Defense" },
      { value: "aerospace", label: "Aerospace" },
      { value: "industrials", label: "Industrials" },
    ],
  },
  {
    label: "Defensive compounders",
    description: "Healthcare, financial rails, consumer quality, utilities.",
    items: [
      { value: "healthcare", label: "Healthcare" },
      { value: "biotech", label: "Biotech" },
      { value: "financials", label: "Financials" },
      { value: "consumer", label: "Consumer" },
      { value: "utilities", label: "Utilities" },
    ],
  },
];

const PROVIDERS: Array<{ value: PortfolioModelProvider; label: string; detail: string }> = [
  { value: "auto", label: "Auto / Best", detail: "GPT -> Opus" },
  { value: "openai", label: "GPT-5.5", detail: "OpenAI" },
  { value: "anthropic", label: "Opus 4.7", detail: "Claude" },
];

export function PortfolioBuilderForm({
  onGenerate,
  loading,
}: {
  onGenerate: (input: PortfolioBuilderInput) => void;
  loading: boolean;
}) {
  const [amountText, setAmountText] = useState("25000");
  const [riskLevel, setRiskLevel] = useState<PortfolioRiskLevel>("balanced");
  const [horizon, setHorizon] = useState<PortfolioTimeHorizon>("1y");
  const [customHorizonLabel, setCustomHorizonLabel] = useState("");
  const [sectors, setSectors] = useState<PortfolioSector[]>([
    "broad_etfs",
    "semiconductors",
    "ai_infrastructure",
  ]);
  const [provider, setProvider] = useState<PortfolioModelProvider>("auto");
  const [researchInstructions, setResearchInstructions] = useState(
    "Scan every eligible name in the chosen themes, avoid generic mega-cap-only output, and explain close omissions.",
  );
  const [preferences, setPreferences] = useState(defaultPortfolioPreferences("balanced"));

  const amount = parseCurrency(amountText);
  const amountValid = Number.isFinite(amount) && amount > 0;
  const formattedAmount = amountValid ? formatUsd(amount) : "Invalid amount";

  const effectivePreferences = useMemo(
    () => ({ ...preferences }),
    [preferences],
  );

  function updateRisk(next: PortfolioRiskLevel) {
    setRiskLevel(next);
    setPreferences((current) => ({
      ...defaultPortfolioPreferences(next),
      includeEtfs: current.includeEtfs,
      includeStocks: current.includeStocks,
      allowFractionalShares: current.allowFractionalShares,
      preferredTickers: current.preferredTickers,
      excludedTickers: current.excludedTickers,
      dividendPreference: current.dividendPreference,
      volatilityTolerance: current.volatilityTolerance,
      rebalanceFrequency: current.rebalanceFrequency,
    }));
  }

  function submit() {
    if (!amountValid) return;
    onGenerate({
      amount,
      horizon,
      customHorizonLabel: horizon === "custom" ? customHorizonLabel : undefined,
      riskLevel,
      sectors,
      researchInstructions: researchInstructions.trim() || undefined,
      modelProvider: provider,
      preferences: effectivePreferences,
    });
  }

  return (
    <Card className="print:hidden">
      <CardHeader>
        <CardTitle>
          <BrainCircuit className="h-3.5 w-3.5 text-primary" />
          Portfolio Construction
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <section className="grid gap-4 md:grid-cols-[1.1fr_1fr]">
          <label className="space-y-2">
            <span className="text-xs uppercase tracking-wider text-muted-foreground">
              Investment amount
            </span>
            <Input
              inputMode="decimal"
              value={amountText}
              onChange={(event) => setAmountText(event.target.value)}
              onBlur={() => amountValid && setAmountText(String(amount))}
              placeholder="$25,000"
              className={cn(!amountValid && "border-short/70 text-short")}
            />
            <span className={cn("block text-xs", amountValid ? "text-muted-foreground" : "text-short")}>
              {formattedAmount}
            </span>
          </label>
          <div className="space-y-2">
            <span className="text-xs uppercase tracking-wider text-muted-foreground">
              Report model
            </span>
            <div className="grid grid-cols-3 border border-border">
              {PROVIDERS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setProvider(option.value)}
                  className={cn(
                    "min-h-12 border-r border-border px-2 py-2 text-left last:border-r-0",
                    provider === option.value
                      ? "bg-primary/15 text-primary"
                      : "text-muted-foreground hover:bg-accent hover:text-foreground",
                  )}
                >
                  <span className="block text-xs font-semibold tracking-wide">
                    {option.label}
                  </span>
                  <span className="mt-1 block truncate text-[9px] uppercase tracking-wider opacity-70">
                    {option.detail}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </section>

        <Segmented
          label="Time horizon"
          value={horizon}
          items={HORIZONS}
          onChange={setHorizon}
        />
        {horizon === "custom" ? (
          <Input
            value={customHorizonLabel}
            onChange={(event) => setCustomHorizonLabel(event.target.value)}
            placeholder="Describe the custom horizon..."
          />
        ) : null}

        <Segmented
          label="Risk / aggressiveness"
          value={riskLevel}
          items={RISKS}
          onChange={updateRisk}
        />

        <section className="space-y-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
            <div>
              <span className="text-xs uppercase tracking-wider text-muted-foreground">
                Research universe
              </span>
              <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                Pick the investable worlds the optimizer should actually compare, not a loose pile of buzzwords.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setSectors([])}
              className="w-fit shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground"
            >
              Reset diversified
            </button>
          </div>
          <div className="grid gap-3">
            {SECTOR_GROUPS.map((group) => (
              <ThemeGroup
                key={group.label}
                group={group}
                selected={sectors}
                onToggle={(sector) =>
                  setSectors((current) =>
                    current.includes(sector)
                      ? current.filter((value) => value !== sector)
                      : [...current, sector],
                  )
                }
              />
            ))}
          </div>
        </section>

        <label className="block space-y-2">
          <span className="text-xs uppercase tracking-wider text-muted-foreground">
            AI selection mandate
          </span>
          <textarea
            value={researchInstructions}
            onChange={(event) => setResearchInstructions(event.target.value)}
            rows={4}
            suppressHydrationWarning
            className="min-h-24 w-full resize-y border border-border bg-background px-3 py-2 text-xs leading-5 text-foreground outline-none ring-offset-background placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            placeholder="Tell GPT/Opus how to audit the universe before writing the report..."
          />
          <span className="block text-[11px] leading-4 text-muted-foreground">
            Sent to the report model with the scored candidate universe, rejected alternatives, constraints, and risk metrics.
          </span>
        </label>

        <details className="group border border-border">
          <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2.5 text-xs uppercase tracking-wider text-cyan">
            Advanced controls
            <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" />
          </summary>
          <div className="grid gap-4 border-t border-border p-4 md:grid-cols-3">
            <NumberField label="Target holdings" value={preferences.targetHoldings} min={1} max={30} step={1} onChange={(targetHoldings) => setPreferences((s) => ({ ...s, targetHoldings }))} />
            <NumberField label="Max per stock %" value={preferences.maxAllocationPerHoldingPct} min={1} max={60} step={1} onChange={(maxAllocationPerHoldingPct) => setPreferences((s) => ({ ...s, maxAllocationPerHoldingPct }))} />
            <NumberField label="Min per stock %" value={preferences.minAllocationPerHoldingPct} min={0} max={25} step={1} onChange={(minAllocationPerHoldingPct) => setPreferences((s) => ({ ...s, minAllocationPerHoldingPct }))} />
            <NumberField label="Max sector %" value={preferences.maxSectorConcentrationPct} min={5} max={80} step={1} onChange={(maxSectorConcentrationPct) => setPreferences((s) => ({ ...s, maxSectorConcentrationPct }))} />
            <NumberField label="Cash reserve %" value={preferences.cashReservePct} min={0} max={80} step={1} onChange={(cashReservePct) => setPreferences((s) => ({ ...s, cashReservePct }))} />
            <SelectField
              label="Rebalance"
              value={preferences.rebalanceFrequency}
              options={["monthly", "quarterly", "semiannual", "annual"]}
              onChange={(rebalanceFrequency) => setPreferences((s) => ({ ...s, rebalanceFrequency }))}
            />
            <Toggle label="Include ETFs" checked={preferences.includeEtfs} onChange={(includeEtfs) => setPreferences((s) => ({ ...s, includeEtfs }))} />
            <Toggle label="Include stocks" checked={preferences.includeStocks} onChange={(includeStocks) => setPreferences((s) => ({ ...s, includeStocks }))} />
            <Toggle label="Fractional shares" checked={preferences.allowFractionalShares} onChange={(allowFractionalShares) => setPreferences((s) => ({ ...s, allowFractionalShares }))} />
            <SelectField
              label="Dividend"
              value={preferences.dividendPreference}
              options={["neutral", "income", "total_return"]}
              onChange={(dividendPreference) => setPreferences((s) => ({ ...s, dividendPreference }))}
            />
            <SelectField
              label="Volatility"
              value={preferences.volatilityTolerance}
              options={["low", "medium", "high"]}
              onChange={(volatilityTolerance) => setPreferences((s) => ({ ...s, volatilityTolerance }))}
            />
            <TickerField label="Preferred tickers" value={preferences.preferredTickers} onChange={(preferredTickers) => setPreferences((s) => ({ ...s, preferredTickers }))} />
            <TickerField label="Excluded tickers" value={preferences.excludedTickers} onChange={(excludedTickers) => setPreferences((s) => ({ ...s, excludedTickers }))} />
          </div>
        </details>

        <Button
          type="button"
          size="lg"
          onClick={submit}
          disabled={!amountValid || loading}
          className="w-full"
        >
          <Sparkles className="h-4 w-4" />
          {loading ? "Generating packet..." : "Generate portfolio report"}
        </Button>
      </CardContent>
    </Card>
  );
}

function Segmented<T extends string>({
  label,
  value,
  items,
  onChange,
}: {
  label: string;
  value: T;
  items: Array<{ value: T; label: string; detail?: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <section className="space-y-2">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <div
        className="grid gap-2"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(104px, 1fr))" }}
      >
        {items.map((item) => (
          <button
            type="button"
            key={item.value}
            onClick={() => onChange(item.value)}
            className={cn(
              "min-h-12 border border-border px-2 py-2 text-left transition-colors",
              value === item.value
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            <span className="block text-xs font-semibold tracking-wider">
              {item.label}
            </span>
            {item.detail ? (
              <span className="mt-1 block text-[9px] uppercase tracking-wider opacity-70">
                {item.detail}
              </span>
            ) : null}
          </button>
        ))}
      </div>
    </section>
  );
}

function ThemeGroup({
  group,
  selected,
  onToggle,
}: {
  group: (typeof SECTOR_GROUPS)[number];
  selected: PortfolioSector[];
  onToggle: (sector: PortfolioSector) => void;
}) {
  return (
    <div className="border border-border p-2">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-cyan">
            {group.label}
          </div>
          <p className="mt-1 text-[10px] leading-4 text-muted-foreground">
            {group.description}
          </p>
        </div>
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
          {group.items.filter((item) => selected.includes(item.value)).length}/{group.items.length}
        </span>
      </div>
      <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(118px, 1fr))" }}>
        {group.items.map((sector) => {
          const active = selected.includes(sector.value);
          return (
            <button
              type="button"
              key={sector.value}
              onClick={() => onToggle(sector.value)}
              className={cn(
                "min-h-10 border px-2 py-1.5 text-left text-[11px] leading-4 tracking-wider transition-colors",
                active
                  ? "border-primary/70 bg-primary/15 text-primary"
                  : "border-border text-muted-foreground hover:border-primary/50 hover:text-foreground",
              )}
            >
              {sector.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="space-y-1.5">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <Input
        type="text"
        inputMode="decimal"
        value={value}
        aria-valuemin={min}
        aria-valuemax={max}
        data-step={step}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: T[];
  onChange: (value: T) => void;
}) {
  return (
    <label className="space-y-1.5">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value as T)}
        className="h-10 w-full border border-border bg-background px-2 text-xs tracking-wider text-foreground"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option.replace("_", " ")}
          </option>
        ))}
      </select>
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={cn(
        "flex h-10 items-center justify-between border px-3 text-xs tracking-wider",
        checked ? "border-primary/60 bg-primary/10 text-primary" : "border-border text-muted-foreground",
      )}
    >
      {label}
      <span>{checked ? "ON" : "OFF"}</span>
    </button>
  );
}

function TickerField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  return (
    <label className="space-y-1.5 md:col-span-3">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <Input
        value={value.join(", ")}
        onChange={(event) =>
          onChange(
            event.target.value
              .split(/[,\s]+/)
              .map((ticker) => ticker.trim().toUpperCase())
              .filter(Boolean),
          )
        }
        placeholder="NVDA, MSFT, SGOV"
      />
    </label>
  );
}

function parseCurrency(value: string): number {
  const cleaned = value.replace(/[$,\s]/g, "");
  if (!cleaned) return Number.NaN;
  return Number(cleaned);
}
