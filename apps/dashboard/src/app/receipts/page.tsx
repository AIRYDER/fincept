"use client";

import { ArrowUpRight, ClipboardCheck, FileJson, ShieldCheck, Terminal } from "lucide-react";
import Link from "next/link";

import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PageHeader } from "@/components/widgets/page-header";
import { buildProofReceiptCenter } from "@/components/receipts/proof-receipts";
import type { ProofReceiptDefinition } from "@/components/receipts/proof-receipts";
import { cn } from "@/lib/utils";

export default function ReceiptsPage() {
  const center = buildProofReceiptCenter();

  return (
    <AppShell>
      <PageHeader
        title="Proof Receipts"
        description="Read-only catalog of exported dashboard receipts and script-generated proof artifacts. This page does not read ignored report folders or run proofs."
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={center.state === "blocked" ? "destructive" : center.state === "review" ? "warn" : "default"}>
              {center.state}
            </Badge>
            <Badge variant="muted">Score {center.score.toFixed(0)}</Badge>
          </div>
        }
      />

      <Card className={cn("mb-4", center.state === "review" && "border-warn/40", center.state === "blocked" && "border-short/45")}>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
            <ClipboardCheck className="h-4 w-4 text-primary" />
            Receipt center readiness
          </CardTitle>
          <CardDescription>
            Catalog coverage, producer mapping, schema labels, and live-stack boundaries.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm leading-6 text-muted-foreground">{center.headline}</p>
          <div className="grid gap-3 md:grid-cols-4">
            <Metric label="Total receipts" value={String(center.stats.total)} tone="primary" />
            <Metric label="Dashboard exports" value={String(center.stats.dashboardExports)} tone="cyan" />
            <Metric label="Local scripts" value={String(center.stats.localScripts)} tone="long" />
            <Metric label="Live scripts" value={String(center.stats.liveScripts)} tone="warn" />
          </div>
          <div className="grid gap-3 lg:grid-cols-[1fr_0.9fr]">
            <div className="space-y-2">
              {center.checks.map((check) => (
                <div key={check.id} className={cn("border p-2", checkClass(check.severity))}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[10px] uppercase tracking-widest text-muted-foreground">{check.label}</span>
                    <span className="font-mono text-[10px] uppercase">{check.severity}</span>
                  </div>
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{check.detail}</p>
                </div>
              ))}
            </div>
            <div className="border border-border/50 bg-background/30 p-3">
              <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                <ShieldCheck className="h-3 w-3 text-cyan" />
                Operator actions
              </div>
              <ul className="space-y-1.5 text-[11px] leading-4 text-muted-foreground">
                {center.actions.map((action) => (
                  <li key={action} className="border-l border-border pl-2">
                    {action}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        {center.receipts.map((receipt) => (
          <ReceiptCard key={receipt.id} receipt={receipt} />
        ))}
      </div>
    </AppShell>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone: "primary" | "cyan" | "long" | "warn" }) {
  return (
    <div className="border border-border/50 bg-background/30 p-3">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={cn("mt-1 font-mono text-2xl font-bold", tone === "primary" && "text-primary", tone === "cyan" && "text-cyan", tone === "long" && "text-long", tone === "warn" && "text-warn")}>{value}</div>
    </div>
  );
}

function ReceiptCard({ receipt }: { receipt: ProofReceiptDefinition }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <FileJson className="h-4 w-4 text-primary" />
              {receipt.title}
            </CardTitle>
            <CardDescription>{receipt.description}</CardDescription>
          </div>
          <Badge variant={receipt.runtime === "live_stack" ? "warn" : "muted"}>{receipt.runtime}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-2 text-xs sm:grid-cols-2">
          <Info label="producer" value={receipt.producer} />
          <Info label="schema" value={receipt.schema} />
          {receipt.reportPath ? <Info label="report path" value={receipt.reportPath} /> : null}
          <Info label="channel" value={receipt.channel} />
        </div>
        {receipt.command ? (
          <div className="border border-border/50 bg-background/40 p-2">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
              <Terminal className="h-3 w-3" />
              Verification command
            </div>
            <code className="block break-all font-mono text-[11px] text-cyan">{receipt.command}</code>
          </div>
        ) : null}
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-widest text-muted-foreground">Proof scope</div>
          <div className="flex flex-wrap gap-1.5">
            {receipt.scope.map((item) => (
              <Badge key={item} variant="outline">{item}</Badge>
            ))}
          </div>
        </div>
        {receipt.liveDependencies.length > 0 ? (
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-widest text-warn">Live dependencies</div>
            <ul className="space-y-1 text-[11px] text-muted-foreground">
              {receipt.liveDependencies.map((dependency) => (
                <li key={dependency} className="border-l border-warn/40 pl-2">{dependency}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {receipt.route ? (
          <Button asChild variant="outline" size="sm">
            <Link href={receipt.route}>
              <ArrowUpRight className="h-3.5 w-3.5" />
              Open producer
            </Link>
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-b border-border/30 pb-1">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className="break-all font-mono text-[11px]">{value}</div>
    </div>
  );
}

function checkClass(severity: "pass" | "watch" | "fail"): string {
  if (severity === "pass") return "border-cyan/30 bg-cyan/5";
  if (severity === "watch") return "border-warn/35 bg-warn/5";
  return "border-short/40 bg-short/5";
}
