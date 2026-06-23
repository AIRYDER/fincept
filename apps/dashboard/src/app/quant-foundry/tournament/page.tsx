"use client";

import { useQuery } from "@tanstack/react-query";
import { Trophy } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { QuantFoundryTournamentEntry } from "@/lib/types";

export default function QuantFoundryTournamentPage() {
  const token = useAuth((s) => s.token);
  const leaderboardQ = useQuery({
    queryKey: ["quant-foundry", "tournament", "leaderboard"],
    queryFn: () => api.quantFoundryTournamentLeaderboard(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const disabled = leaderboardQ.error instanceof UnavailableError && leaderboardQ.error.status === 503;
  const entries = leaderboardQ.data ?? [];

  return (
    <AppShell>
      <PageHeader
        title="Quant Foundry Tournament"
        description="Expanded leaderboard with baseline deltas, blocking issue visibility, calibration, and decay flags."
        action={<StatusPill intent={disabled ? "inactive" : "verified"} label={disabled ? "DISABLED" : "LEADERBOARD"} />}
      />

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Trophy className="h-4 w-4 text-primary" />
            Ranked leaderboard
          </CardTitle>
          <CardDescription>Flagged models are pushed below clean entries by the Python tournament.</CardDescription>
        </CardHeader>
        <CardContent>
          {disabled ? (
            <EmptyState title="Quant Foundry is disabled" body="No tournament entries are available while the gateway is disabled." />
          ) : leaderboardQ.isLoading ? (
            <EmptyState title="Loading leaderboard" body="Reading expanded tournament entries." />
          ) : leaderboardQ.error ? (
            <EmptyState title="Unable to load leaderboard" body={leaderboardQ.error instanceof Error ? leaderboardQ.error.message : "Unknown error"} />
          ) : entries.length === 0 ? (
            <EmptyState title="No leaderboard entries" body="No settled tournament results have been added to the expanded leaderboard." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-border/40 text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Rank</th>
                    <th className="py-2 pr-3 font-medium">Model</th>
                    <th className="py-2 pr-3 font-medium">Score</th>
                    <th className="py-2 pr-3 font-medium">Baseline delta</th>
                    <th className="py-2 pr-3 font-medium">Settled</th>
                    <th className="py-2 pr-3 font-medium">Decay flags</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/30">
                  {entries.map((entry, index) => (
                    <tr key={entry.model_id}>
                      <td className="py-2 pr-3 tabular-nums">{index + 1}</td>
                      <td className="py-2 pr-3 font-mono text-foreground">{entry.model_id}</td>
                      <td className="py-2 pr-3 tabular-nums">{formatScore(entry.total_score)}</td>
                      <td className="py-2 pr-3 tabular-nums">{formatDelta(entry)}</td>
                      <td className="py-2 pr-3 tabular-nums text-muted-foreground">{entry.settled_count}</td>
                      <td className="py-2 pr-3"><DecayStatus entry={entry} /></td>
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

function formatScore(value: number): string {
  return value.toFixed(4);
}

function formatDelta(entry: QuantFoundryTournamentEntry): string {
  if (entry.baseline_delta === null) return "—";
  const sign = entry.baseline_delta.delta >= 0 ? "+" : "";
  return `${sign}${entry.baseline_delta.delta.toFixed(4)} vs ${entry.baseline_delta.baseline_model_id}`;
}

function DecayStatus({ entry }: { readonly entry: QuantFoundryTournamentEntry }) {
  const decay = entry.decay_indicator;
  if (decay === null) return <StatusPill intent="verified" label="CLEAN" compact />;
  if (decay.is_decayed) return <StatusPill intent="critical" label="DECAYED" compact />;
  if (decay.is_stale) return <StatusPill intent="degraded" label="STALE" compact />;
  return <StatusPill intent="verified" label={`FRESH ${decay.days_since_last_settlement}D`} compact />;
}

function EmptyState({ title, body }: { readonly title: string; readonly body: string }) {
  return <div className="rounded-md border border-border/30 bg-card/40 p-6 text-center"><p className="text-sm font-medium">{title}</p><p className="mt-1 text-xs text-muted-foreground">{body}</p></div>;
}
