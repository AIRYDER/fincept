"use client";

import { Download, FileJson, FileText, SearchCheck, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KpiTile } from "@/components/widgets/kpi-tile";
import { cn, formatUsd } from "@/lib/utils";

import { PortfolioAllocationTable } from "./PortfolioAllocationTable";
import { PortfolioCharts } from "./PortfolioCharts";
import { OptimizerControlTower } from "./OptimizerControlTower";
import { PortfolioOptimizerCockpit } from "./PortfolioOptimizerCockpit";
import { ScenarioWarRoomPanel } from "./ScenarioWarRoomPanel";
import {
  buildPortfolioPdfFilename,
  downloadTextFile,
  openPortfolioPdfPrintDialog,
  portfolioToCsv,
  portfolioToJson,
} from "./portfolioExport";
import type {
  PortfolioAllocationResult,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export function PortfolioReportView({
  allocation,
  report,
}: {
  allocation: PortfolioAllocationResult;
  report: PortfolioReportLLMResponse;
}) {
  const timestamp = new Date(allocation.marketData.timestamp).toLocaleString();
  const pdfFilename = buildPortfolioPdfFilename(allocation);
  const cashBreakdown = `reserve ${formatUsd(allocation.summary.cashReserve)} / rounding ${formatUsd(allocation.summary.roundingCash)}`;

  return (
    <div className="print-ink-save print-report-page space-y-4 print:space-y-3">
      <div className="hidden print:block print:border-b print:border-black print:pb-3">
        <div className="text-[10px] uppercase tracking-[0.2em] text-black">
          Fincept Terminal
        </div>
        <h1 className="mt-1 text-2xl font-bold text-black">
          Investment Committee Packet
        </h1>
        <p className="mt-1 text-xs text-black">
          Generated {timestamp} · {report.providerLabel} · proposed allocation only
        </p>
        <p className="mt-1 text-[10px] text-black">
          PDF export target: {pdfFilename}
        </p>
      </div>

      <Card className="break-inside-avoid border-primary/40 print:border-border">
        <CardHeader className="flex-row items-center justify-between gap-3 print:text-black">
          <CardTitle>
            <ShieldCheck className="h-3.5 w-3.5 text-primary" />
            Investment Committee Packet
          </CardTitle>
          <div className="flex flex-wrap items-center gap-2 print:hidden">
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                downloadTextFile(
                  "portfolio-allocation.json",
                  portfolioToJson(allocation),
                  "application/json",
                )
              }
            >
              <FileJson className="h-3.5 w-3.5" />
              JSON
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                downloadTextFile(
                  "portfolio-allocation.csv",
                  portfolioToCsv(allocation),
                  "text/csv",
                )
              }
            >
              <Download className="h-3.5 w-3.5" />
              CSV
            </Button>
            <Button variant="outline" size="sm" onClick={() => openPortfolioPdfPrintDialog(allocation)}>
              <FileText className="h-3.5 w-3.5" />
              PDF / Print
            </Button>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr] print:block print:bg-white print:text-black">
          <section className="space-y-2">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Executive summary
            </div>
            <p className="text-sm leading-6 text-foreground">{report.executiveSummary}</p>
            <p className="text-xs leading-5 text-muted-foreground">{report.portfolioReasoning}</p>
          </section>
          <section className="border border-border p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Portfolio confidence
              </span>
              <span className="font-mono text-lg text-primary">
                {allocation.summary.confidenceScore.toFixed(0)}
              </span>
            </div>
            <div className="h-2 border border-border bg-background">
              <div
                className="h-full bg-primary"
                style={{ width: `${allocation.summary.confidenceScore}%` }}
              />
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <Metric label="Model" value={report.providerLabel} />
              <Metric label="Data" value={allocation.marketData.dataMode === "demo" ? "Demo market data" : "Live market data"} />
              <Metric label="Timestamp" value={timestamp} />
              <Metric label="Rebalance" value={allocation.summary.suggestedRebalanceFrequency} />
            </dl>
            {report.providerDiagnostics?.length ? (
              <div className="mt-3 border border-warn/40 bg-warn/5 p-2 text-[11px] leading-5 text-warn">
                {report.providerDiagnostics.join(" ")}
              </div>
            ) : null}
          </section>
        </CardContent>
      </Card>

      {allocation.marketData.dataMode === "demo" ? (
        <div className="border border-warn/40 bg-warn/5 px-3 py-2 text-xs text-warn">
          Demo market data is active. The allocation is mathematically deterministic, but prices are placeholders until live quote adapters are connected.
        </div>
      ) : null}

      <OptimalityBreakdown allocation={allocation} report={report} />

      <PortfolioOptimizerCockpit allocation={allocation} report={report} />

      <div className="print:hidden">
        <OptimizerControlTower allocation={allocation} />
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <KpiTile label="Starting amount" value={formatUsd(allocation.summary.startingAmount)} />
        <KpiTile label="Total invested" value={formatUsd(allocation.summary.totalInvested)} />
        <KpiTile label="Uninvested cash" value={formatUsd(allocation.summary.totalCash)} sub={cashBreakdown} />
        <KpiTile label="Positions" value={String(allocation.summary.numberOfPositions)} sub={`${allocation.summary.largestHoldingTicker ?? "N/A"} largest`} />
        <KpiTile label="Largest holding" value={`${allocation.summary.largestHoldingPct.toFixed(1)}%`} />
        <KpiTile label="Largest sector" value={`${allocation.summary.largestSectorExposurePct.toFixed(1)}%`} sub={allocation.summary.largestSector ?? "N/A"} />
        <KpiTile label="ETF / stock" value={`${allocation.summary.etfPct.toFixed(0)} / ${allocation.summary.stockPct.toFixed(0)}%`} />
        <KpiTile label="Diversification" value={allocation.summary.diversificationScore.toFixed(0)} />
      </div>

      <PortfolioAllocationTable holdings={allocation.holdings} />

      <PortfolioCharts
        sectorAllocations={allocation.sectorAllocations}
        assetTypeAllocations={allocation.assetTypeAllocations}
        riskBuckets={allocation.riskBuckets}
        holdings={allocation.holdings}
      />

      <ScenarioWarRoomPanel allocation={allocation} />

      <div className="grid gap-3 lg:grid-cols-2">
        <TextPanel title="Optimality review">{report.optimalityReview}</TextPanel>
        <TextPanel title="Universe review">{report.universeReview}</TextPanel>
        <TextPanel title="Time horizon explanation">
          {report.timeHorizonExplanation}
        </TextPanel>
        <TextPanel title="Rebalancing plan">{report.rebalancingPlan}</TextPanel>
        <TextPanel title="Risk analysis">{report.riskAnalysis}</TextPanel>
        <section className="border border-border">
          <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
            Risk checklist
          </div>
          <div className="grid gap-2 p-3 text-xs leading-5 text-muted-foreground">
            {Object.entries(allocation.riskAnalysis).map(([key, value]) => (
              <div key={key}>
                <span className="mr-2 text-foreground">{humanize(key)}:</span>
                {value}
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {allocation.holdings.map((holding) => (
          <article key={holding.ticker} className="border border-border p-3">
            <div className="mb-2 flex items-start justify-between gap-3">
              <div>
                <h3 className="font-mono text-lg text-foreground">{holding.ticker}</h3>
                <p className="text-xs text-muted-foreground">{holding.name}</p>
              </div>
              <span
                className={cn(
                  "border px-1.5 py-0.5 text-[10px] uppercase tracking-wider",
                  holding.riskRating === "Low" && "border-long/40 text-long",
                  holding.riskRating === "Medium" && "border-cyan/40 text-cyan",
                  holding.riskRating === "High" && "border-warn/40 text-warn",
                  holding.riskRating === "Speculative" && "border-short/40 text-short",
                )}
              >
                {holding.riskRating}
              </span>
            </div>
            <p className="text-xs leading-5 text-muted-foreground">
              {report.holdingRationales[holding.ticker] ?? holding.reason}
            </p>
            <div className="mt-3 border-t border-border pt-2 text-xs text-muted-foreground">
              <span className="text-foreground">Key risk:</span> {holding.keyRisk}
            </div>
          </article>
        ))}
      </section>

      <section className="border border-border p-3">
        <div className="mb-2 text-[10px] uppercase tracking-wider text-cyan">
          Assumptions and limitations
        </div>
        <ul className="grid gap-1 text-xs leading-5 text-muted-foreground md:grid-cols-2">
          {[...allocation.assumptions, ...report.assumptionsAndLimitations].slice(0, 12).map((item, index) => (
            <li key={`${item}-${index}`}>{item}</li>
          ))}
        </ul>
        {report.researchMandate.length ? (
          <div className="mt-3 border-t border-border pt-3">
            <div className="mb-2 text-[10px] uppercase tracking-wider text-cyan">
              Next research mandate
            </div>
            <ul className="grid gap-1 text-xs leading-5 text-muted-foreground md:grid-cols-2">
              {report.researchMandate.slice(0, 8).map((item, index) => (
                <li key={`${item}-${index}`}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
        <p className="mt-3 border-t border-border pt-3 text-[11px] leading-5 text-muted-foreground">
          This report is not financial advice, does not execute trades, and does not connect to Alpaca, Schwab, IBKR, or any brokerage execution workflow.
        </p>
      </section>
    </div>
  );
}

function OptimalityBreakdown({
  allocation,
  report,
}: {
  allocation: PortfolioAllocationResult;
  report: PortfolioReportLLMResponse;
}) {
  return (
    <section className="break-inside-avoid border border-border">
      <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-cyan">
          <SearchCheck className="h-3.5 w-3.5" />
          Optimality Breakdown
        </div>
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          {allocation.candidateAudit.eligibleCount} eligible · {allocation.candidateAudit.selectedCount} selected
        </span>
      </div>
      <div className="grid gap-3 p-3 lg:grid-cols-[1fr_1fr] print:block">
        <div className="space-y-3">
          <p className="text-xs leading-5 text-muted-foreground">
            {report.optimalityReview}
          </p>
          <div className="grid gap-2 md:grid-cols-2">
            {allocation.candidateAudit.constraintNotes.map((note) => (
              <div key={note} className="border border-border px-2 py-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">
                {note}
              </div>
            ))}
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-2 print:mt-3">
          <CandidateStack title="Selected winners" rows={allocation.candidateAudit.topSelected.slice(0, 6)} />
          <CandidateStack title="Close omissions" rows={allocation.candidateAudit.topRejected.slice(0, 6)} />
        </div>
      </div>
      {report.agentDebate.length ? (
        <div className="border-t border-border p-3">
          <div className="mb-2 text-[10px] uppercase tracking-wider text-cyan">
            Agent council notes
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {report.agentDebate.slice(0, 6).map((entry) => (
              <div key={`${entry.agent}-${entry.finding}`} className="border border-border p-2">
                <div className="text-[10px] uppercase tracking-wider text-foreground">
                  {entry.agent}
                </div>
                <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                  {entry.finding}
                </p>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function CandidateStack({
  title,
  rows,
}: {
  title: string;
  rows: PortfolioAllocationResult["candidateAudit"]["topSelected"];
}) {
  return (
    <div className="border border-border">
      <div className="border-b border-border px-2 py-1.5 text-[10px] uppercase tracking-wider text-cyan">
        {title}
      </div>
      <div className="divide-y divide-border">
        {rows.map((row) => (
          <div key={`${title}-${row.ticker}`} className="grid grid-cols-[64px_1fr_auto] gap-2 px-2 py-1.5 text-[11px]">
            <span className="font-mono text-foreground">{row.ticker}</span>
            <span className="truncate text-muted-foreground">{row.theme}</span>
            <span className="font-mono text-primary">{row.score.toFixed(0)}</span>
          </div>
        ))}
        {!rows.length ? (
          <div className="px-2 py-2 text-[11px] text-muted-foreground">
            No rows after constraints.
          </div>
        ) : null}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</dt>
      <dd className="truncate font-mono text-foreground">{value}</dd>
    </div>
  );
}

function TextPanel({ title, children }: { title: string; children: string }) {
  return (
    <section className="border border-border">
      <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
        {title}
      </div>
      <p className="p-3 text-xs leading-5 text-muted-foreground">{children}</p>
    </section>
  );
}

function humanize(value: string): string {
  return value.replace(/([A-Z])/g, " $1").toLowerCase();
}
