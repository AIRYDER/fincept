"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity } from "lucide-react";
import { useMemo, useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import type { QuantFoundryJob } from "@/lib/types";

const JOB_STATUSES = ["all", "queued", "running", "retrying", "failed", "completed"] as const;

export default function QuantFoundryJobsPage() {
  const token = useAuth((s) => s.token);
  const [status, setStatus] = useState<(typeof JOB_STATUSES)[number]>("all");
  const statusArg = status === "all" ? undefined : status;
  const jobsQ = useQuery({
    queryKey: ["quant-foundry", "jobs", statusArg ?? "all"],
    queryFn: () => api.quantFoundryJobs(token, { status: statusArg }),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const disabled = jobsQ.error instanceof UnavailableError && jobsQ.error.status === 503;
  const sortedJobs = useMemo(() => sortJobs(jobsQ.data ?? []), [jobsQ.data]);

  return (
    <AppShell>
      <PageHeader
        title="Quant Foundry Jobs"
        description="Read-only outbox view across queued, running, retrying, failed, and completed Quant Foundry jobs."
        action={<StatusPill intent={disabled ? "inactive" : "verified"} label={disabled ? "DISABLED" : "READ ONLY"} />}
      />

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div>
              <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
                <Activity className="h-4 w-4 text-primary" />
                Job outbox
              </CardTitle>
              <CardDescription>Filter by lifecycle state. Disabled is a safe empty state.</CardDescription>
            </div>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              Status
              <select
                value={status}
                onChange={(event) => setStatus(parseJobStatus(event.target.value))}
                className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground"
              >
                {JOB_STATUSES.map((item) => (
                  <option key={item} value={item}>{item.toUpperCase()}</option>
                ))}
              </select>
            </label>
          </div>
        </CardHeader>
        <CardContent>
          {disabled ? (
            <EmptyState title="Quant Foundry is disabled" body="No jobs are created or processed while the gateway is absent or disabled." />
          ) : jobsQ.isLoading ? (
            <EmptyState title="Loading jobs" body="Reading the Quant Foundry outbox." />
          ) : jobsQ.error ? (
            <EmptyState title="Unable to load jobs" body={jobsQ.error instanceof Error ? jobsQ.error.message : "Unknown error"} />
          ) : sortedJobs.length === 0 ? (
            <EmptyState title="No jobs" body="The selected outbox state has no jobs." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-border/40 text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Job ID</th>
                    <th className="py-2 pr-3 font-medium">Type</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 pr-3 font-medium">Priority</th>
                    <th className="py-2 pr-3 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/30">
                  {sortedJobs.map((job) => (
                    <tr key={job.job_id}>
                      <td className="max-w-[280px] truncate py-2 pr-3 font-mono text-foreground">{job.job_id}</td>
                      <td className="py-2 pr-3 text-muted-foreground">{job.job_type}</td>
                      <td className="py-2 pr-3"><StatusPill intent={jobIntent(job.status)} label={job.status.toUpperCase()} compact /></td>
                      <td className="py-2 pr-3 tabular-nums">{job.priority ?? 0}</td>
                      <td className="py-2 pr-3 tabular-nums text-muted-foreground">{formatNs(job.updated_at_ns ?? job.created_at_ns)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </AppShell>
  );
}

function sortJobs(jobs: readonly QuantFoundryJob[]): QuantFoundryJob[] {
  return [...jobs].sort((a, b) => (b.updated_at_ns ?? b.created_at_ns ?? 0) - (a.updated_at_ns ?? a.created_at_ns ?? 0));
}

function jobIntent(status: string): SemanticIntent {
  if (status === "completed") return "verified";
  if (status === "failed" || status === "rejected") return "critical";
  if (status === "retrying") return "degraded";
  if (status === "queued" || status === "running") return "degraded";
  return "inactive";
}

function parseJobStatus(value: string): (typeof JOB_STATUSES)[number] {
  switch (value) {
    case "all":
    case "queued":
    case "running":
    case "retrying":
    case "failed":
    case "completed":
      return value;
    default:
      return "all";
  }
}

function formatNs(value: number | undefined): string {
  if (value === undefined) return "—";
  return new Date(Math.floor(value / 1_000_000)).toLocaleString();
}

function EmptyState({ title, body }: { readonly title: string; readonly body: string }) {
  return <div className="rounded-md border border-border/30 bg-card/40 p-6 text-center"><p className="text-sm font-medium">{title}</p><p className="mt-1 text-xs text-muted-foreground">{body}</p></div>;
}
