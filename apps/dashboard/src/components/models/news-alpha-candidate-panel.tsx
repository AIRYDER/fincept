"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  FileText,
  XCircle,
} from "lucide-react";

import { PromoteButton } from "@/components/models/promote-button";
import { ShadowButton } from "@/components/models/shadow-button";
import { EmptyState } from "@/components/widgets/empty-state";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { NewsAlphaCandidateReport } from "@/lib/types";

const NEWS_ALPHA_AGENT = "news_alpha_predictor.v1";

export function NewsAlphaCandidatePanel() {
  const token = useAuth((s) => s.token);
  const report = useQuery({
    queryKey: ["models", "news-alpha", "candidate-report"],
    queryFn: () => api.newsAlphaCandidateReport(token),
    enabled: !!token,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const payload = report.data?.report ?? null;

  return (
    <Card className="mt-6">
      <CardHeader className="flex flex-row items-start justify-between gap-3 pb-3">
        <div>
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <BrainCircuit className="h-4 w-4 text-primary" />
            News-alpha candidate gate
          </CardTitle>
          <CardDescription>
            Latest offline evaluation report for the scheduled news-alpha candidate.
            Operators can set shadow or active bindings manually after the gate
            passes.
          </CardDescription>
        </div>
        {payload ? (
          <Badge variant={payload.approved ? "long" : "destructive"}>
            {payload.approved ? "Approved" : "Blocked"}
          </Badge>
        ) : report.data?.exists === false ? (
          <Badge variant="muted">No report</Badge>
        ) : null}
      </CardHeader>
      <CardContent>
        {report.error ? (
          <EmptyState
            icon={AlertTriangle}
            title="Failed to load candidate report"
            description={
              report.error instanceof Error
                ? report.error.message
                : "Unknown error"
            }
          />
        ) : report.isLoading ? (
          <EmptyState
            icon={FileText}
            title="Loading candidate report…"
            description="Polling /models/news-alpha/candidate-report."
          />
        ) : payload ? (
          <CandidateReportView
            report={payload}
            reportPath={report.data?.report_path ?? ""}
          />
        ) : (
          <EmptyState
            icon={FileText}
            title="No candidate report yet"
            description={`Scheduled training will write ${
              report.data?.report_path ?? "reports/news_alpha_candidate_report.json"
            } after export, train, and evaluate complete.`}
          />
        )}
      </CardContent>
    </Card>
  );
}

function CandidateReportView({
  report,
  reportPath,
}: {
  report: NewsAlphaCandidateReport;
  reportPath: string;
}) {
  const candidateAuc = numericMeta(report.candidate_meta, "best_auc");
  const activeAuc = report.active_meta
    ? numericMeta(report.active_meta, "best_auc")
    : null;
  const rows = numericMeta(report.candidate_meta, "rows");
  const valRows = numericMeta(report.candidate_meta, "val_rows");
  const minAuc = numericMeta(report.policy, "min_auc");
  const minRows = numericMeta(report.policy, "min_rows");
  const minValRows = numericMeta(report.policy, "min_val_rows");

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <MetricTile
          label="Candidate AUC"
          value={formatNumber(candidateAuc, 3)}
          hint={
            minAuc == null
              ? "policy unavailable"
              : `min ${formatNumber(minAuc, 3)}`
          }
          tone={
            candidateAuc != null && minAuc != null && candidateAuc >= minAuc
              ? "long"
              : "warn"
          }
        />
        <MetricTile
          label="Training rows"
          value={formatNumber(rows, 0)}
          hint={
            minRows == null
              ? "policy unavailable"
              : `min ${formatNumber(minRows, 0)}`
          }
          tone={
            rows != null && minRows != null && rows >= minRows ? "long" : "warn"
          }
        />
        <MetricTile
          label="Validation rows"
          value={formatNumber(valRows, 0)}
          hint={
            minValRows == null
              ? "policy unavailable"
              : `min ${formatNumber(minValRows, 0)}`
          }
          tone={
            valRows != null && minValRows != null && valRows >= minValRows
              ? "long"
              : "warn"
          }
        />
        <MetricTile
          label="Active AUC"
          value={formatNumber(activeAuc, 3)}
          hint={report.active_model_name ?? "no active model"}
          tone="muted"
        />
      </div>

      <div className="rounded-md border border-border/40 bg-background/30 p-3">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              {report.approved ? (
                <CheckCircle2 className="h-4 w-4 text-long" />
              ) : (
                <XCircle className="h-4 w-4 text-destructive" />
              )}
              <code className="truncate font-mono text-sm font-semibold">
                {report.candidate_model_name}
              </code>
              <Badge variant="muted">{NEWS_ALPHA_AGENT}</Badge>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1">
                <Clock3 className="h-3 w-3" />
                generated {formatTimestamp(report.generated_at)}
              </span>
              <span className="font-mono">{reportPath}</span>
            </div>
            <div className="text-xs text-muted-foreground">
              Candidate directory:{" "}
              <code className="font-mono">{report.candidate_dir}</code>
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-stretch gap-2 md:items-end">
            <ShadowButton
              modelName={report.candidate_model_name}
              agentId={NEWS_ALPHA_AGENT}
              compact
            />
            <PromoteButton
              modelName={report.candidate_model_name}
              agentId={NEWS_ALPHA_AGENT}
              compact
            />
            {!report.approved ? (
              <div className="max-w-48 text-right text-[11px] text-destructive">
                Gate is blocked; actions are manual operator override.
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {report.reasons.length > 0 ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
          <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-destructive">
            <AlertTriangle className="h-3.5 w-3.5" />
            Gate reasons
          </div>
          <ul className="list-disc space-y-1 pl-5 text-xs text-destructive/90">
            {report.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function MetricTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone: "long" | "warn" | "muted";
}) {
  const classes = {
    long: "border-long/30 bg-long/5 text-long",
    warn: "border-warn/30 bg-warn/5 text-warn",
    muted: "border-border/40 bg-background/30 text-muted-foreground",
  }[tone];
  return (
    <div className={`rounded-md border p-3 ${classes}`}>
      <div className="text-[10px] uppercase tracking-widest">{label}</div>
      <div className="mt-1 font-mono text-2xl font-bold">{value}</div>
      <div className="text-[11px] opacity-80">{hint}</div>
    </div>
  );
}

function numericMeta(
  meta: Record<string, unknown>,
  key: string,
): number | null {
  const value = meta[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatNumber(value: number | null, digits: number): string {
  if (value == null) return "—";
  return value.toFixed(digits);
}

function formatTimestamp(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return "unknown";
  return new Date(epochSeconds * 1000).toLocaleString();
}
