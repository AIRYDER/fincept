"use client";

import {
  AlertTriangle,
  BrainCircuit,
  CheckCircle2,
  Info,
  Newspaper,
  Shield,
} from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type {
  NewsImpactStatus,
  NewsResponse,
  Position,
  PromotionStateResponse,
  ServicesResponse,
} from "@/lib/types";
import type {
  NewsAlphaCandidateReportResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  buildNewsIntelligence,
  type NewsIntelSummary,
} from "./news-intelligence";

// ---------------------------------------------------------------------------
// Severity helpers
// ---------------------------------------------------------------------------

function severityColor(severity: NewsIntelSummary["checks"][number]["severity"]) {
  return severity === "fail"
    ? "text-short"
    : severity === "watch"
      ? "text-warn"
      : "text-long";
}

function severityIcon(severity: NewsIntelSummary["checks"][number]["severity"]) {
  if (severity === "fail") return <AlertTriangle className="h-3.5 w-3.5 text-short" />;
  if (severity === "watch") return <Info className="h-3.5 w-3.5 text-warn" />;
  return <CheckCircle2 className="h-3.5 w-3.5 text-long" />;
}

function stateLabel(state: NewsIntelSummary["state"]) {
  if (state === "ready") return <Badge variant="long">READY</Badge>;
  if (state === "review") return <Badge variant="warn">REVIEW</Badge>;
  return <Badge variant="short">BLOCKED</Badge>;
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function NewsIntelligencePanel({
  news,
  impactStatus,
  positions,
  promotion,
  newsAlphaReport,
  services,
}: {
  news: NewsResponse | null;
  impactStatus: NewsImpactStatus | null;
  positions: Position[];
  promotion?: PromotionStateResponse | null;
  newsAlphaReport?: NewsAlphaCandidateReportResponse | null;
  services?: ServicesResponse | null;
}) {
  const summary = buildNewsIntelligence({
    news,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport,
    services,
  });

  return (
    <Card className="mb-4">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-2">
        <div className="flex items-center gap-2">
          <BrainCircuit className="h-3.5 w-3.5 text-primary" />
          <span className="text-[11px] font-semibold uppercase tracking-wider">
            News Intelligence
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
          <MiniStat label="Alerts" value={String(summary.stats.alertCount)} warn={summary.stats.alertCount > 0} />
          <MiniStat label="Impact stories" value={String(summary.stats.impactCount)} />
          <MiniStat label="Book symbols" value={String(summary.stats.bookSymbols)} />
          <MiniStat
            label="Impact model"
            value={summary.stats.impactModelLoaded ? "Loaded" : "Not loaded"}
            warn={!summary.stats.impactModelLoaded}
          />
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

        {/* News-alpha promotion gate */}
        <div className="flex items-center gap-2 border-t border-border/40 pt-2 text-xs">
          <Shield className="h-3 w-3 text-muted-foreground" />
          <span className="font-medium">News-alpha</span>
          {summary.stats.newsAlphaPromoted ? (
            <Badge variant="long">Promoted</Badge>
          ) : summary.stats.newsAlphaCandidateApproved === false ? (
            <Badge variant="warn">Candidate not approved</Badge>
          ) : (
            <Badge variant="muted">No candidate</Badge>
          )}
          {summary.stats.labelCoverage && (
            <span className="text-muted-foreground">· {summary.stats.labelCoverage}</span>
          )}
        </div>

        {/* Symbol posture */}
        {summary.symbols.length > 0 ? (
          <div className="border-t border-border/40 pt-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Symbol posture · {summary.symbols.length} symbols
            </div>
            <div className="flex flex-wrap gap-1">
              {summary.symbols.slice(0, 30).map((row) => (
                <Link
                  key={row.symbol}
                  href={`/positions`}
                  className={cn(
                    "inline-flex items-center gap-1 px-1.5 py-[1px] font-mono text-[10px]",
                    row.inBook
                      ? row.alertCount > 0
                        ? "bg-short/15 text-short"
                        : "bg-long/10 text-long"
                      : "bg-muted/30 text-muted-foreground",
                  )}
                >
                  {row.symbol}
                  {row.alertCount > 0 && (
                    <AlertTriangle className="h-2.5 w-2.5" />
                  )}
                </Link>
              ))}
              {summary.symbols.length > 30 && (
                <span className="px-1 text-[10px] text-muted-foreground">
                  +{summary.symbols.length - 30}
                </span>
              )}
            </div>
          </div>
        ) : null}

        {/* Actions */}
        {summary.actions.length > 0 ? (
          <div className="border-t border-border/40 pt-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Suggested checks
            </div>
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {summary.actions.map((action, i) => (
                <li key={i} className="flex items-start gap-1.5">
                  <Newspaper className="mt-0.5 h-3 w-3 shrink-0" />
                  {action}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Caveat */}
        <p className="text-[10px] text-muted-foreground">
          Read-only intelligence layer. Impact model predictions are experimental.
          News-alpha cannot be presented as executable without promotion evidence.
        </p>

        {/* Provider freshness (TASK-0205) — redacted evidence backed */}
        {services && services.services && services.services.length > 0 && (
          <div className="border-t border-border/40 pt-2">
            <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Provider freshness (evidence receipts)
            </div>
            <div className="flex flex-wrap gap-2 text-[10px]">
              {services.services
                .filter((s) => /provider|alpaca|news|data|openbb|polygon/i.test(s.name))
                .slice(0, 6)
                .map((s) => {
                  const age = s.age_sec != null ? Math.round(s.age_sec) : null;
                  const isStale = age != null && age > 30;
                  return (
                    <span
                      key={s.name}
                      className={cn(
                        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5",
                        isStale ? "border-warn/40 text-warn" : "border-border/30 text-muted-foreground"
                      )}
                      aria-label={`${s.name} ${age ?? "?"}s ago`}
                    >
                      {s.name}: {age != null ? `${age}s` : "—"} {isStale ? "stale" : ""}
                    </span>
                  );
                })}
              {services.services.filter((s) => /provider|alpaca|news|data|openbb|polygon/i.test(s.name)).length === 0 && (
                <span className="text-muted-foreground">No provider heartbeats in services</span>
              )}
            </div>
            <div className="mt-1 text-[9px] text-muted-foreground">
              Freshness from redacted provider evidence receipts. See /research/provider-data for details.
            </div>
          </div>
        )}
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
      <div className={cn("mt-0.5 font-mono text-sm", warn && "text-warn")}>
        {value}
      </div>
    </div>
  );
}
