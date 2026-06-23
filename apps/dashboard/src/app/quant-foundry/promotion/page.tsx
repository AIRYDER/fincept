"use client";

import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

import { AppShell } from "@/components/shell/app-shell";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { QuantFoundryPromotionQueueEntry, QuantFoundryPromotionReview } from "@/lib/types";

export default function QuantFoundryPromotionPage() {
  const token = useAuth((s) => s.token);
  const queueQ = useQuery({
    queryKey: ["quant-foundry", "promotion", "queue"],
    queryFn: () => api.quantFoundryPromotionQueue(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const completedQ = useQuery({
    queryKey: ["quant-foundry", "promotion", "completed"],
    queryFn: () => api.quantFoundryPromotionCompleted(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const disabled = (queueQ.error instanceof UnavailableError && queueQ.error.status === 503)
    || (completedQ.error instanceof UnavailableError && completedQ.error.status === 503);
  const pending = queueQ.data ?? [];
  const completed = completedQ.data ?? [];

  return (
    <AppShell>
      <PageHeader
        title="Quant Foundry Promotion"
        description="Read-only promotion queue with review packets, confirmation preview, completed receipts, and rollback visibility notes."
        action={<StatusPill intent={disabled ? "inactive" : "verified"} label={disabled ? "DISABLED" : "READ ONLY"} />}
      />

      <div className="grid gap-4 xl:grid-cols-[1.3fr_1fr]">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <ShieldCheck className="h-4 w-4 text-primary" />
              Pending review queue
            </CardTitle>
            <CardDescription>Approve/reject controls are confirmation previews only. No POST endpoint is wired in TASK-0802.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {disabled ? (
              <EmptyState title="Quant Foundry is disabled" body="Promotion review is unavailable until the gateway is configured." />
            ) : queueQ.isLoading ? (
              <EmptyState title="Loading review queue" body="Reading pending promotion packets." />
            ) : queueQ.error ? (
              <EmptyState title="Unable to load promotion queue" body={queueQ.error instanceof Error ? queueQ.error.message : "Unknown error"} />
            ) : pending.length === 0 ? (
              <EmptyState title="No pending reviews" body="No model promotion requests are waiting for human review." />
            ) : (
              pending.map((entry) => <PendingReviewCard key={entry.request.model_id} entry={entry} />)
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="normal-case tracking-normal">Completed promotions</CardTitle>
            <CardDescription>Receipts show approval/rejection and retain rollback context through the request target.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {disabled ? (
              <EmptyState title="Quant Foundry is disabled" body="No completed receipts are available." />
            ) : completedQ.isLoading ? (
              <EmptyState title="Loading receipts" body="Reading completed promotion decisions." />
            ) : completedQ.error ? (
              <EmptyState title="Unable to load receipts" body={completedQ.error instanceof Error ? completedQ.error.message : "Unknown error"} />
            ) : completed.length === 0 ? (
              <EmptyState title="No completed promotions" body="Approved and rejected promotion receipts will appear here." />
            ) : (
              completed.map((review) => <CompletedReviewRow key={`${review.request.model_id}-${review.decided_at_ns}`} review={review} />)
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}

function PendingReviewCard({ entry }: { readonly entry: QuantFoundryPromotionQueueEntry }) {
  const issueCount = entry.evidence.blocking_issues.length;
  return (
    <div className="rounded-md border border-border/30 bg-card/40 p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <p className="font-mono text-sm text-foreground">{entry.request.model_id}</p>
          <p className="mt-1 text-xs text-muted-foreground">Target: {entry.request.target_level}. Evidence: {evidenceSummary(entry)}.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusPill intent={issueCount > 0 ? "critical" : "verified"} label={`${issueCount} BLOCKERS`} compact />
          <ReviewDialog entry={entry} decision="approve" />
          <ReviewDialog entry={entry} decision="reject" />
        </div>
      </div>
      <p className="mt-3 rounded-md border border-border/30 bg-background/40 p-2 text-xs text-muted-foreground">{entry.request.review_note}</p>
    </div>
  );
}

function ReviewDialog({ entry, decision }: { readonly entry: QuantFoundryPromotionQueueEntry; readonly decision: "approve" | "reject" }) {
  return (
    <Dialog>
      <DialogTrigger className="rounded-md border border-border px-2 py-1 text-xs uppercase tracking-wide transition-colors hover:bg-muted/40">
        {decision}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{decision === "approve" ? "Approve preview" : "Reject preview"}</DialogTitle>
          <DialogDescription>Server-side promotion is pending. TASK-0802 intentionally ships no write endpoint.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2 text-sm">
          <p><span className="text-muted-foreground">Model:</span> {entry.request.model_id}</p>
          <p><span className="text-muted-foreground">Target:</span> {entry.request.target_level}</p>
          <p><span className="text-muted-foreground">Evidence packet:</span> {evidenceSummary(entry)}</p>
          <p className="rounded-md border border-border/30 bg-background/40 p-2 text-xs text-muted-foreground">Rollback visibility remains tied to the current model pointer and the completed promotion receipt once server-side promotion writes exist.</p>
        </div>
        <DialogClose className="rounded-md border border-border px-3 py-2 text-sm hover:bg-muted/40">Close</DialogClose>
      </DialogContent>
    </Dialog>
  );
}

function CompletedReviewRow({ review }: { readonly review: QuantFoundryPromotionReview }) {
  const rejected = review.decision === "rejected";
  return (
    <div className="rounded-md border border-border/30 bg-card/40 p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-foreground">{review.request.model_id}</span>
        <StatusPill intent={rejected ? "critical" : "verified"} label={review.decision.toUpperCase()} compact />
      </div>
      <p className="mt-1 text-muted-foreground">Target {review.request.target_level}. {review.rejection_reason ?? "Rollback receipt visible after server-side pointer writes."}</p>
    </div>
  );
}

function evidenceSummary(entry: QuantFoundryPromotionQueueEntry): string {
  const dossier = entry.evidence.dossier === null ? "no dossier" : "dossier ready";
  const tournament = entry.evidence.tournament_result === null ? "no tournament" : "tournament ready";
  const sentinel = entry.evidence.sentinel_receipt === null ? "no sentinel" : "sentinel ready";
  return `${dossier}, ${tournament}, ${sentinel}, ${entry.evidence.blocking_issues.length} blocking issues`;
}

function EmptyState({ title, body }: { readonly title: string; readonly body: string }) {
  return <div className="rounded-md border border-border/30 bg-card/40 p-6 text-center"><p className="text-sm font-medium">{title}</p><p className="mt-1 text-xs text-muted-foreground">{body}</p></div>;
}
