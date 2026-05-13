"use client";

import { AlertTriangle, BrainCircuit } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";

import {
  PortfolioAgentConsole,
  type PortfolioAgentEvent,
} from "./PortfolioAgentConsole";
import { PortfolioBuilderForm } from "./PortfolioBuilderForm";
import { PortfolioReportView } from "./PortfolioReportView";
import { generatePortfolioReport } from "./portfolioModelProvider";
import { buildPortfolioAllocation } from "./portfolioOptimizer";
import type {
  PortfolioAllocationResult,
  PortfolioBuilderInput,
  PortfolioReportLLMResponse,
} from "./portfolioBuilder.types";

export function PortfolioBuilderPage() {
  const [allocation, setAllocation] = useState<PortfolioAllocationResult | null>(null);
  const [report, setReport] = useState<PortfolioReportLLMResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agentEvents, setAgentEvents] = useState<PortfolioAgentEvent[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<PortfolioBuilderInput["modelProvider"]>("auto");
  const reportRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!report) return;
    requestAnimationFrame(() => {
      reportRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }, [report]);

  async function generate(input: PortfolioBuilderInput) {
    setLoading(true);
    setError(null);
    setSelectedProvider(input.modelProvider);
    setAgentEvents([
      event("Universe Scout", "running", `Scanning selected universe: ${input.sectors.length ? input.sectors.join(", ") : "broad diversified default"}.`),
      event("Constraint Solver", "queued", "Waiting for scored candidates, cash reserve, caps, and share conversion."),
      event("Report Council", "queued", `Standing by for ${input.modelProvider === "auto" ? "Auto / Best" : input.modelProvider}.`),
    ]);
    try {
      const nextAllocation = buildPortfolioAllocation(input);
      setAllocation(nextAllocation);
      setReport(null);
      setAgentEvents((current) => [
        ...current,
        event(
          "Universe Scout",
          "complete",
          `Scored ${nextAllocation.candidateAudit.eligibleCount} eligible candidates from ${nextAllocation.candidateAudit.universeCount}; selected ${nextAllocation.candidateAudit.selectedCount}.`,
        ),
        event(
          "Constraint Solver",
          nextAllocation.optimization.feasible ? "complete" : "warning",
          `${nextAllocation.optimization.method} optimizer returned ${nextAllocation.optimization.feasible ? "a feasible" : "an infeasible"} packet with ${nextAllocation.optimization.bindingConstraints.length || 0} binding constraints.`,
        ),
        event(
          "Risk Officer",
          "complete",
          `Largest holding ${nextAllocation.summary.largestHoldingTicker ?? "N/A"} at ${nextAllocation.summary.largestHoldingPct.toFixed(1)}%; largest sector ${nextAllocation.summary.largestSector ?? "N/A"} at ${nextAllocation.summary.largestSectorExposurePct.toFixed(1)}%.`,
        ),
        event("Report Council", "running", "Sending locked math, candidate audit, and user mandate to the report model."),
      ]);
      const nextReport = await generatePortfolioReport(nextAllocation, input.modelProvider);
      setReport(nextReport);
      setAgentEvents((current) => [
        ...current,
        event(
          "Report Council",
          nextReport.fallbackUsed ? "warning" : "complete",
          `${nextReport.providerLabel} returned a sanitized committee packet with optimality and universe-review sections.`,
        ),
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to generate portfolio.");
      setAgentEvents((current) => [
        ...current,
        event("Report Council", "warning", err instanceof Error ? err.message : "Portfolio generation failed."),
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <AppShell>
      <div className="print:hidden">
        <PageHeader
          title="AI Portfolio Builder"
          description="Generate a deterministic allocation and an AI-readable investment committee packet. This tool proposes portfolios only; it does not place trades."
          action={
            <Badge variant={allocation?.marketData.dataMode === "demo" ? "warn" : "muted"}>
              {allocation?.marketData.dataMode === "demo" ? "Demo data" : "Optimizer"}
            </Badge>
          }
        />
      </div>

      <div className="grid gap-4 print:block xl:grid-cols-[520px_1fr]">
        <div className="space-y-4">
          <PortfolioBuilderForm onGenerate={generate} loading={loading} />
          <PortfolioAgentConsole
            events={agentEvents}
            loading={loading}
            provider={selectedProvider}
            allocation={allocation}
            report={report}
          />
          {error ? (
            <div className="flex gap-2 border border-short/40 bg-short/5 p-3 text-xs text-short">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {error}
            </div>
          ) : null}
        </div>

        <div ref={reportRef} className="min-w-0">
          {allocation && report ? (
            <PortfolioReportView allocation={allocation} report={report} />
          ) : loading && allocation ? (
            <Card>
              <CardContent className="flex min-h-[520px] flex-col items-center justify-center gap-4 border border-dashed border-primary/50 text-center">
                <div className="flex h-12 w-12 items-center justify-center border border-primary/50 bg-primary/10 text-primary">
                  <BrainCircuit className="h-5 w-5 animate-pulse" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold">Analyzing allocation with report provider</h2>
                  <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
                    The deterministic portfolio math is complete. The server is now asking the selected AI provider for a deeper investment committee analysis without changing prices, weights, dollars, or shares. This can take a couple minutes on high reasoning.
                  </p>
                </div>
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardContent className="flex min-h-[520px] flex-col items-center justify-center gap-4 border border-dashed border-border/80 text-center">
                <div className="flex h-12 w-12 items-center justify-center border border-border bg-card text-primary">
                  <BrainCircuit className="h-5 w-5" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold">Ready for portfolio construction</h2>
                  <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
                    Enter capital, select horizon and risk controls, then generate a full allocation packet with share counts, risk scoring, charts, assumptions, and export options.
                  </p>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </AppShell>
  );
}

function event(
  agent: string,
  status: PortfolioAgentEvent["status"],
  message: string,
): PortfolioAgentEvent {
  return {
    at: new Date().toISOString(),
    agent,
    status,
    message,
  };
}
