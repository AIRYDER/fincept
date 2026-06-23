"use client";

import { useQuery } from "@tanstack/react-query";
import { Archive } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { SemanticIntent } from "@/lib/design-tokens";
import type { QuantFoundryDossier } from "@/lib/types";

export default function QuantFoundryModelsPage() {
  const token = useAuth((s) => s.token);
  const dossiersQ = useQuery({
    queryKey: ["quant-foundry", "dossiers"],
    queryFn: () => api.quantFoundryDossiers(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const disabled = dossiersQ.error instanceof UnavailableError && dossiersQ.error.status === 503;
  const dossiers = dossiersQ.data ?? [];

  return (
    <AppShell>
      <PageHeader
        title="Quant Foundry Models"
        description="Dossier registry view with artifact hashes, lifecycle status, evidence completeness, and blocking issues."
        action={<StatusPill intent={disabled ? "inactive" : "verified"} label={disabled ? "DISABLED" : "DOSSIERS"} />}
      />

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <Archive className="h-4 w-4 text-primary" />
            Dossier registry
          </CardTitle>
          <CardDescription>Immutable model dossiers backed by artifact and evidence references.</CardDescription>
        </CardHeader>
        <CardContent>
          {disabled ? (
            <EmptyState title="Quant Foundry is disabled" body="The dossier registry is unavailable until the gateway is configured." />
          ) : dossiersQ.isLoading ? (
            <EmptyState title="Loading dossiers" body="Reading model evidence records." />
          ) : dossiersQ.error ? (
            <EmptyState title="Unable to load dossiers" body={dossiersQ.error instanceof Error ? dossiersQ.error.message : "Unknown error"} />
          ) : dossiers.length === 0 ? (
            <EmptyState title="No dossiers" body="No candidate models have registered reproducibility records yet." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-border/40 text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Model</th>
                    <th className="py-2 pr-3 font-medium">Artifact hash</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 pr-3 font-medium">Evidence</th>
                    <th className="py-2 pr-3 font-medium">Blocking issues</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/30">
                  {dossiers.map((dossier) => (
                    <tr key={dossier.model_id}>
                      <td className="py-2 pr-3 font-mono text-foreground">{dossier.model_id}</td>
                      <td className="py-2 pr-3 font-mono text-muted-foreground">{truncateHash(dossier.artifact_sha256)}</td>
                      <td className="py-2 pr-3"><StatusPill intent={statusIntent(dossier.status)} label={dossier.status.toUpperCase()} compact /></td>
                      <td className="py-2 pr-3 tabular-nums">{evidenceCount(dossier)} refs</td>
                      <td className="py-2 pr-3 tabular-nums text-muted-foreground">{dossier.blocking_issues.length}</td>
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

function evidenceCount(dossier: QuantFoundryDossier): number {
  return dossier.settlement_evidence_refs.length + dossier.shadow_prediction_refs.length;
}

function truncateHash(value: string): string {
  return value.length <= 18 ? value : `${value.slice(0, 10)}…${value.slice(-6)}`;
}

function statusIntent(status: string): SemanticIntent {
  if (status === "rejected") return "critical";
  if (status === "candidate") return "degraded";
  return "verified";
}

function EmptyState({ title, body }: { readonly title: string; readonly body: string }) {
  return <div className="rounded-md border border-border/30 bg-card/40 p-6 text-center"><p className="text-sm font-medium">{title}</p><p className="mt-1 text-xs text-muted-foreground">{body}</p></div>;
}
