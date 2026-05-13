"use client";

import { AlertTriangle, ClipboardCheck, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { ModelRecord, PromotionStateResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import { buildModelDossier } from "./model-dossier";

type Dossier = ReturnType<typeof buildModelDossier>;
type Check = Dossier["checks"][number];

export function ModelDossierPanel({
  model,
  promotion,
}: {
  model: ModelRecord;
  promotion?: PromotionStateResponse | null;
}) {
  const dossier = buildModelDossier(model, promotion ?? null);

  return (
    <Card
      className={cn(
        "mt-6",
        dossier.state === "ready" && "border-cyan/35",
        dossier.state === "review" && "border-warn/40",
        dossier.state === "blocked" && "border-short/45",
      )}
    >
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
          {dossier.state === "blocked" ? (
            <AlertTriangle className="h-4 w-4 text-short" />
          ) : (
            <ShieldCheck className="h-4 w-4 text-primary" />
          )}
          Model validation dossier
        </CardTitle>
        <CardDescription>
          Read-only promotion review packet built from model metadata and binding state.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={dossier.state === "blocked" ? "destructive" : dossier.state === "review" ? "warn" : "default"}>
            {dossier.state}
          </Badge>
          <Badge variant="muted">Score {dossier.score.toFixed(0)}</Badge>
          <Badge variant="outline">Read only</Badge>
        </div>
        <p className="text-sm leading-6 text-muted-foreground">{dossier.headline}</p>
        <div className="grid gap-3 lg:grid-cols-[1fr_0.75fr]">
          <div className="space-y-2">
            {dossier.checks.map((check) => (
              <DossierCheck key={check.id} check={check} />
            ))}
          </div>
          <div className="space-y-3">
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                <ClipboardCheck className="h-3 w-3 text-cyan" />
                Evidence
              </div>
              <div className="space-y-1.5">
                {dossier.evidence.map((row) => (
                  <div key={row.label} className="flex items-baseline justify-between gap-3 border-b border-border/25 pb-1 text-xs">
                    <span className="uppercase tracking-widest text-muted-foreground">{row.label}</span>
                    <span className={cn("font-mono", row.severity === "fail" && "text-short", row.severity === "watch" && "text-warn", row.severity === "pass" && "text-cyan")}>
                      {row.value}
                    </span>
                  </div>
                ))}
              </div>
            </div>
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                Operator actions
              </div>
              <ul className="space-y-1.5 text-[11px] leading-4 text-muted-foreground">
                {dossier.actions.map((action) => (
                  <li key={action} className="border-l border-border pl-2">
                    {action}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function DossierCheck({ check }: { check: Check }) {
  return (
    <div className={cn("border p-2", checkClass(check.severity))}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">{check.label}</span>
        <span className="font-mono text-[10px] uppercase">{check.severity}</span>
      </div>
      <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{check.detail}</p>
    </div>
  );
}

function checkClass(severity: Check["severity"]): string {
  if (severity === "pass") return "border-cyan/30 bg-cyan/5";
  if (severity === "watch") return "border-warn/35 bg-warn/5";
  return "border-short/40 bg-short/5";
}
