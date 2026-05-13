"use client";

import { AlertTriangle, Activity, Download, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn, formatPercent, formatUsd, pnlClass } from "@/lib/utils";

import type { PortfolioAllocationResult } from "./portfolioBuilder.types";
import { downloadTextFile } from "./portfolioExport";
import {
  STRESS_REGIMES,
  buildWarRoomReceipt,
  groupStressRegimesByPolarity,
  runPortfolioStress,
  warRoomReceiptToJson,
  type StressHoldingResult,
  type StressRegime,
  type StressRegimeId,
  type StressRegimePolarity,
  type StressSeverity,
} from "./war-room";

const SEVERITIES: Array<{ value: StressSeverity; label: string }> = [
  { value: "mild", label: "Mild" },
  { value: "base", label: "Base" },
  { value: "severe", label: "Severe" },
];

const REGIME_GROUPS: Array<{
  key: StressRegimePolarity;
  label: string;
  description: string;
}> = [
  {
    key: "upside",
    label: "Upside regimes",
    description: "What wins if the tape gets better.",
  },
  {
    key: "downside",
    label: "Downside regimes",
    description: "What breaks if stress broadens.",
  },
  {
    key: "mixed",
    label: "Mixed shocks",
    description: "Dispersion trades with winners and losers.",
  },
];

export function ScenarioWarRoomPanel({
  allocation,
}: {
  allocation: PortfolioAllocationResult;
}) {
  const [regimeId, setRegimeId] = useState<StressRegimeId>("recession");
  const [severity, setSeverity] = useState<StressSeverity>("base");
  const result = useMemo(
    () => runPortfolioStress(allocation, { regimeId, severity }),
    [allocation, regimeId, severity],
  );
  const receipt = useMemo(
    () => buildWarRoomReceipt(allocation, result),
    [allocation, result],
  );
  const groupedRegimes = useMemo(() => groupStressRegimesByPolarity(STRESS_REGIMES), []);
  const selectedRegime = STRESS_REGIMES.find((regime) => regime.id === regimeId) ?? STRESS_REGIMES[0];

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <CardTitle>
          <Activity className="h-3.5 w-3.5 text-cyan" />
          Scenario War Room
        </CardTitle>
        <div className="flex flex-wrap gap-1 print:hidden">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() =>
              downloadTextFile(
                `scenario-war-room-${result.regimeId}.json`,
                warRoomReceiptToJson(receipt),
                "application/json",
              )
            }
            className="h-7 px-2 text-[10px]"
          >
            <Download className="h-3.5 w-3.5" />
            JSON
          </Button>
          {SEVERITIES.map((option) => (
            <Button
              key={option.value}
              type="button"
              variant={severity === option.value ? "default" : "outline"}
              size="sm"
              onClick={() => setSeverity(option.value)}
              className="h-7 px-2 text-[10px]"
            >
              {option.label}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 xl:grid-cols-[320px_1fr] print:block">
          <div className="space-y-3 print:hidden">
            {REGIME_GROUPS.map((group) => {
              const regimes = groupedRegimes[group.key];
              return (
                <section key={group.key} className="border border-border/80 p-2">
                  <div className="mb-2 flex items-start justify-between gap-3">
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-cyan">
                        {group.label}
                      </div>
                      <p className="mt-1 text-[10px] leading-4 text-muted-foreground">
                        {group.description}
                      </p>
                    </div>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {regimes.length}
                    </span>
                  </div>
                  <div className="grid gap-1">
                    {regimes.map((regime) => (
                      <RegimeButton
                        key={regime.id}
                        regime={regime}
                        active={regimeId === regime.id}
                        onClick={() => setRegimeId(regime.id)}
                      />
                    ))}
                  </div>
                </section>
              );
            })}
          </div>

          <div className="space-y-3">
            <div className="border border-border p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  Active regime
                </div>
                <PolarityBadge polarity={selectedRegime.polarity} />
                <span className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                  {selectedRegime.category}
                </span>
              </div>
              <div className="text-sm font-medium text-foreground">{selectedRegime.label}</div>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {selectedRegime.description}
              </p>
            </div>

            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <Metric label="Starting value" value={formatUsd(result.startingValue)} />
              <Metric label="Stressed value" value={formatUsd(result.stressedValue)} />
              <Metric
                label="Estimated P&L"
                value={formatUsd(result.pnlDelta, { signed: true })}
                tone={pnlClass(result.pnlDelta)}
              />
              <Metric
                label="Estimated P&L %"
                value={formatPercent(result.pnlDeltaPct, 2)}
                tone={pnlClass(result.pnlDeltaPct)}
              />
            </div>
          </div>
        </div>

        {result.guardrailBreaches.length ? (
          <div className="grid gap-2 md:grid-cols-2">
            {result.guardrailBreaches.map((breach) => (
              <div
                key={`${breach.id}-${breach.message}`}
                className={cn(
                  "flex gap-2 border p-2 text-xs leading-5",
                  breach.severity === "critical" && "border-short/40 bg-short/5 text-short",
                  breach.severity === "warn" && "border-warn/40 bg-warn/5 text-warn",
                  breach.severity === "info" && "border-cyan/40 bg-cyan/5 text-cyan",
                )}
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{breach.message}</span>
              </div>
            ))}
          </div>
        ) : null}

        <div className="grid gap-3 lg:grid-cols-2">
          <ContributorTable
            title="Worst contributors"
            icon={<TrendingDown className="h-3.5 w-3.5 text-short" />}
            holdings={result.worstContributors}
          />
          <ContributorTable
            title="Best contributors"
            icon={<TrendingUp className="h-3.5 w-3.5 text-long" />}
            holdings={result.bestContributors}
          />
        </div>

        {result.warnings.length ? (
          <div className="border border-warn/40 bg-warn/5 p-2 text-[11px] leading-5 text-warn">
            {result.warnings.join(" ")}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function RegimeButton({
  regime,
  active,
  onClick,
}: {
  regime: StressRegime;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "border px-3 py-2 text-left text-xs transition-colors",
        active
          ? polarityActiveClass(regime.polarity)
          : "border-border text-muted-foreground hover:border-cyan/40 hover:text-foreground",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-medium">{regime.label}</div>
          <div className="mt-1 text-[10px] uppercase tracking-wider opacity-70">
            {regime.category}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <PolarityBadge polarity={regime.polarity} />
          <span className="font-mono text-[10px] uppercase opacity-70">
            {regime.defaultSeverity}
          </span>
        </div>
      </div>
    </button>
  );
}

function PolarityBadge({ polarity }: { polarity: StressRegimePolarity }) {
  return (
    <span
      className={cn(
        "border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
        polarity === "upside" && "border-long/40 bg-long/10 text-long",
        polarity === "downside" && "border-short/40 bg-short/10 text-short",
        polarity === "mixed" && "border-warn/40 bg-warn/10 text-warn",
      )}
    >
      {polarity}
    </span>
  );
}

function polarityActiveClass(polarity: StressRegimePolarity) {
  if (polarity === "upside") return "border-long/70 bg-long/10 text-long";
  if (polarity === "downside") return "border-short/70 bg-short/10 text-short";
  return "border-warn/70 bg-warn/10 text-warn";
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <div className="border border-border p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-1 font-mono text-lg text-foreground", tone)}>
        {value}
      </div>
    </div>
  );
}

function ContributorTable({
  title,
  icon,
  holdings,
}: {
  title: string;
  icon: React.ReactNode;
  holdings: StressHoldingResult[];
}) {
  return (
    <section className="border border-border">
      <div className="flex items-center gap-1 border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
        {icon}
        {title}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
            <tr className="border-b border-border">
              <th className="px-3 py-2 font-medium">Ticker</th>
              <th className="px-3 py-2 font-medium">Shock</th>
              <th className="px-3 py-2 font-medium">P&L</th>
              <th className="px-3 py-2 font-medium">Contribution</th>
            </tr>
          </thead>
          <tbody>
            {holdings.map((holding) => (
              <tr key={holding.ticker} className="border-b border-border/60 last:border-0">
                <td className="px-3 py-2 font-mono text-foreground">{holding.ticker}</td>
                <td className={cn("px-3 py-2 font-mono", pnlClass(holding.appliedShockPct))}>
                  {formatPercent(holding.appliedShockPct, 1)}
                </td>
                <td className={cn("px-3 py-2 font-mono", pnlClass(holding.pnlDelta))}>
                  {formatUsd(holding.pnlDelta, { signed: true })}
                </td>
                <td className="px-3 py-2 font-mono text-muted-foreground">
                  {holding.contributionPct.toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
