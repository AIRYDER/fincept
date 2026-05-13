"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FlaskConical,
  Info,
  Shield,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type { BacktestManifest, BacktestReport } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  backtestLabReceiptFilename,
  buildBacktestLab,
  buildBacktestLabReceipt,
  type BacktestLabReceipt,
  type BacktestLabSummary,
} from "./backtest-lab";

// ---------------------------------------------------------------------------
// Severity helpers
// ---------------------------------------------------------------------------

function severityColor(severity: BacktestLabSummary["checks"][number]["severity"]) {
  return severity === "fail"
    ? "text-short"
    : severity === "watch"
      ? "text-yellow-500"
      : "text-long";
}

function severityIcon(severity: BacktestLabSummary["checks"][number]["severity"]) {
  if (severity === "fail") return <AlertTriangle className="h-3.5 w-3.5 text-short" />;
  if (severity === "watch") return <Info className="h-3.5 w-3.5 text-yellow-500" />;
  return <CheckCircle2 className="h-3.5 w-3.5 text-long" />;
}

function stateLabel(state: BacktestLabSummary["state"]) {
  if (state === "ready") return <Badge variant="long">READY</Badge>;
  if (state === "review") return <Badge variant="warn">REVIEW</Badge>;
  return <Badge variant="short">BLOCKED</Badge>;
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function BacktestLabPanel({
  report,
  manifest,
}: {
  report: BacktestReport | null;
  manifest: BacktestManifest | null;
}) {
  const summary = buildBacktestLab({ report, manifest });
  const receipt = buildBacktestLabReceipt({ report, manifest });

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-2">
        <div className="flex items-center gap-2">
          <FlaskConical className="h-3.5 w-3.5 text-primary" />
          <span className="text-[11px] font-semibold uppercase tracking-wider">
            Backtest Lab
          </span>
          {stateLabel(summary.state)}
        </div>
        <span className="text-[10px] text-muted-foreground">
          Score {summary.score} · {summary.headline}
        </span>
      </div>
      <CardContent className="space-y-3 px-4 py-3">
        {/* Stats row */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <MiniStat label="Fills" value={String(summary.stats.nFills)} />
          <MiniStat
            label="Fee impact"
            value={`${summary.stats.feeImpactPct.toFixed(2)}%`}
            warn={summary.stats.feeImpactPct > 1}
          />
          <MiniStat
            label="Turnover"
            value={`${summary.stats.turnoverRatio.toFixed(1)}x`}
            warn={summary.stats.turnoverRatio > 20}
          />
          <MiniStat label="Symbols" value={String(summary.stats.symbolsTraded)} />
        </div>

        {/* Checks */}
        <div className="space-y-1">
          {summary.checks.map((check) => (
            <div key={check.id} className="flex items-start gap-2 text-xs">
              {severityIcon(check.severity)}
              <span className={cn("font-medium", severityColor(check.severity))}>
                {check.label}
              </span>
              <span className="text-muted-foreground">{check.detail}</span>
            </div>
          ))}
        </div>

        {/* Per-symbol attribution */}
        {summary.attribution.length > 0 ? (
          <div className="border-t border-border/40 pt-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Attribution · {summary.attribution.length} symbols
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  <tr className="border-b border-border/40">
                    <th className="px-2 py-1 text-left">Symbol</th>
                    <th className="px-2 py-1 text-right">Fills</th>
                    <th className="px-2 py-1 text-right">Notional</th>
                    <th className="px-2 py-1 text-right">Fees</th>
                    <th className="px-2 py-1 text-right">Fee bps</th>
                    <th className="px-2 py-1 text-right">% total</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.attribution.map((row) => (
                    <tr key={row.symbol} className="border-b border-border/20 last:border-0">
                      <td className="px-2 py-1 font-mono">{row.symbol}</td>
                      <td className="px-2 py-1 text-right font-mono">{row.fills}</td>
                      <td className="px-2 py-1 text-right font-mono">
                        {row.notionalTraded >= 1000
                          ? `$${(row.notionalTraded / 1000).toFixed(1)}k`
                          : `$${row.notionalTraded.toFixed(0)}`}
                      </td>
                      <td className="px-2 py-1 text-right font-mono">${row.feesPaid.toFixed(2)}</td>
                      <td className={cn("px-2 py-1 text-right font-mono", row.feeBps > 10 && "text-yellow-500")}>
                        {row.feeBps.toFixed(1)}
                      </td>
                      <td className="px-2 py-1 text-right font-mono">{row.pctOfTotalNotional.toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        {/* Assumptions */}
        {summary.assumptions.length > 0 ? (
          <div className="border-t border-border/40 pt-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Assumptions
            </div>
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {summary.assumptions.map((a, i) => (
                <li key={i} className="font-mono">{a}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Risk gate caveat */}
        <div className="flex items-center gap-2 border-t border-border/40 pt-2 text-xs">
          <Shield className="h-3 w-3 text-muted-foreground" />
          <span className="font-medium">Risk gate</span>
          <Badge variant="warn">Not simulated</Badge>
          <span className="text-muted-foreground">Rejected trades are invisible in backtest</span>
        </div>

        {/* Actions */}
        {summary.actions.length > 0 ? (
          <div className="border-t border-border/40 pt-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Suggested checks
            </div>
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {summary.actions.map((action, i) => (
                <li key={i} className="flex items-start gap-1.5">
                  <FlaskConical className="mt-0.5 h-3 w-3 shrink-0" />
                  {action}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Receipt export */}
        {receipt ? (
          <div className="border-t border-border/40 pt-2">
            <button
              onClick={() => downloadReceipt(receipt)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-background/40 px-3 py-1.5 text-xs hover:bg-accent/30"
            >
              <Download className="h-3 w-3" />
              Export run receipt
            </button>
            <span className="ml-2 text-[10px] text-muted-foreground">
              {backtestLabReceiptFilename(receipt)}
            </span>
          </div>
        ) : null}

        {/* Caveat */}
        <p className="text-[10px] text-muted-foreground">
          Read-only analysis layer. Backtest results do not reflect live execution
          risk, slippage, or risk-gate rejections. Compare with paper-spine replay
          for end-to-end validation.
        </p>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Mini stat
// ---------------------------------------------------------------------------

function MiniStat({
  label,
  value,
  warn = false,
}: {
  label: string;
  value: string;
  warn?: boolean;
}) {
  return (
    <div className="border border-border/40 bg-card/50 p-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-0.5 font-mono text-sm", warn && "text-yellow-500")}>
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Receipt download helper
// ---------------------------------------------------------------------------

function downloadReceipt(receipt: BacktestLabReceipt) {
  const json = JSON.stringify(receipt, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = backtestLabReceiptFilename(receipt);
  a.click();
  URL.revokeObjectURL(url);
}
