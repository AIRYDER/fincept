"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Info,
  Shield,
  Wrench,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type {
  DataCoverageRow,
  OrderRecord,
  Position,
  ServicesResponse,
  StrategyConfigRow,
  StrategyRow,
  UniverseRow,
} from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  buildReconChecklist,
  buildReconReceipt,
  reconReceiptFilename,
  type ReconChecklistSummary,
  type ReconIssue,
  type ReconReceipt,
} from "./recon-checklist";

// ---------------------------------------------------------------------------
// Severity helpers
// ---------------------------------------------------------------------------

function severityColor(severity: ReconIssue["severity"]) {
  if (severity === "critical") return "text-short";
  if (severity === "warning") return "text-yellow-500";
  return "text-muted-foreground";
}

function severityIcon(severity: ReconIssue["severity"]) {
  if (severity === "critical") return <AlertTriangle className="h-3.5 w-3.5 text-short" />;
  if (severity === "warning") return <Info className="h-3.5 w-3.5 text-yellow-500" />;
  return <CheckCircle2 className="h-3.5 w-3.5 text-long" />;
}

function ownerBadge(owner: ReconIssue["owner"]) {
  const variants: Record<ReconIssue["owner"], React.ComponentProps<typeof Badge>["variant"]> = {
    operator: "default",
    data: "secondary",
    strategy: "outline",
    risk: "destructive",
    broker: "muted",
  };
  return <Badge variant={variants[owner]}>{owner}</Badge>;
}

function stateLabel(state: ReconChecklistSummary["state"]) {
  if (state === "clean") return <Badge variant="long">CLEAN</Badge>;
  if (state === "attention") return <Badge variant="warn">ATTENTION</Badge>;
  return <Badge variant="short">CRITICAL</Badge>;
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function ReconChecklistPanel({
  positions,
  strategies,
  configs,
  universe,
  coverage,
  orders,
  services,
}: {
  positions: Position[];
  strategies: StrategyRow[];
  configs: StrategyConfigRow[];
  universe: UniverseRow[];
  coverage: DataCoverageRow[];
  orders: OrderRecord[];
  services?: ServicesResponse | null;
}) {
  const summary = buildReconChecklist({
    positions,
    strategies,
    configs,
    universe,
    coverage,
    orders,
    services,
  });

  const receipt = buildReconReceipt(summary);

  return (
    <Card className="mb-4 border-primary/20">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-2">
        <div className="flex items-center gap-2">
          <Shield className="h-3.5 w-3.5 text-primary" />
          <span className="text-[11px] font-semibold uppercase tracking-wider">
            Daily Checklist
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
          <MiniStat label="Open positions" value={String(summary.stats.openPositions)} />
          <MiniStat label="Pending orders" value={String(summary.stats.pendingOrders)} warn={summary.stats.pendingOrders > 0} />
          <MiniStat label="Rejected orders" value={String(summary.stats.rejectedOrders)} warn={summary.stats.rejectedOrders > 0} />
          <MiniStat label="Services down" value={String(summary.stats.servicesDown)} warn={summary.stats.servicesDown > 0} />
        </div>

        {/* Issues list */}
        {summary.issues.length > 0 ? (
          <div className="space-y-1.5">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Issues · {summary.stats.criticalCount} critical · {summary.stats.warningCount} warning
            </div>
            {summary.issues.map((issue) => (
              <div key={issue.id} className="flex items-start gap-2 text-xs">
                {severityIcon(issue.severity)}
                <span className={cn("font-medium shrink-0", severityColor(issue.severity))}>
                  {issue.label}
                </span>
                {ownerBadge(issue.owner)}
                <span className="text-muted-foreground flex-1">{issue.detail}</span>
                {issue.repairAction && (
                  <span className="flex items-center gap-1 shrink-0 text-muted-foreground">
                    <Wrench className="h-3 w-3" />
                    {issue.repairAction}
                  </span>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="flex items-center gap-2 text-xs text-long">
            <CheckCircle2 className="h-3.5 w-3.5" />
            All reconciliation checks passed
          </div>
        )}

        {/* Summary stats */}
        <div className="grid grid-cols-2 gap-2 border-t border-border/40 pt-2 text-xs sm:grid-cols-5">
          <div>
            <span className="text-muted-foreground">Missing configs: </span>
            <span className={cn(summary.stats.missingConfigs > 0 && "text-short")}>
              {summary.stats.missingConfigs}
            </span>
          </div>
          <div>
            <span className="text-muted-foreground">Missing runtimes: </span>
            <span className={cn(summary.stats.missingRuntimes > 0 && "text-yellow-500")}>
              {summary.stats.missingRuntimes}
            </span>
          </div>
          <div>
            <span className="text-muted-foreground">Universe gaps: </span>
            <span className={cn(summary.stats.missingUniverse > 0 && "text-yellow-500")}>
              {summary.stats.missingUniverse}
            </span>
          </div>
          <div>
            <span className="text-muted-foreground">Coverage gaps: </span>
            <span className={cn(summary.stats.coverageGaps > 0 && "text-yellow-500")}>
              {summary.stats.coverageGaps}
            </span>
          </div>
          <div>
            <span className="text-muted-foreground">Strategy groups: </span>
            <span>{summary.stats.strategyGroups}</span>
          </div>
        </div>

        {/* Receipt export */}
        <div className="border-t border-border/40 pt-2">
          <button
            onClick={() => downloadReceipt(receipt)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-background/40 px-3 py-1.5 text-xs hover:bg-accent/30"
          >
            <Download className="h-3 w-3" />
            Export daily checklist
          </button>
          <span className="ml-2 text-[10px] text-muted-foreground">
            {reconReceiptFilename(receipt)}
          </span>
        </div>
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
// Receipt download
// ---------------------------------------------------------------------------

function downloadReceipt(receipt: ReconReceipt) {
  const json = JSON.stringify(receipt, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = reconReceiptFilename(receipt);
  a.click();
  URL.revokeObjectURL(url);
}
