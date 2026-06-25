"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Rocket,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useState } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/widgets/page-header";
import { StatusPill } from "@/components/widgets/status-pill";
import { ApiError, api, UnavailableError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  QuantFoundryPromotionQueueEntry,
  QuantFoundryPromotionReview,
} from "@/lib/types";

const PROMOTION_LEVELS = [
  "candidate",
  "research_approved",
  "shadow_approved",
] as const;

const REJECTION_REASONS = [
  "no_dossier",
  "insufficient_evidence",
  "sentinel_failed",
  "blocking_issue",
  "mvp_level_limit",
] as const;

export default function QuantFoundryPromotionPage() {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
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

  const [submitModelId, setSubmitModelId] = useState("");
  const [submitTargetLevel, setSubmitTargetLevel] = useState<string>(PROMOTION_LEVELS[2]);
  const [submitReviewNote, setSubmitReviewNote] = useState("");
  const [submitSuccess, setSubmitSuccess] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const submitMutation = useMutation({
    mutationFn: () =>
      api.quantFoundrySubmitPromotion(token, {
        model_id: submitModelId,
        target_level: submitTargetLevel,
        review_note: submitReviewNote,
      }),
    onSuccess: (data) => {
      setSubmitError(null);
      setSubmitSuccess(
        `Submitted ${data.entry?.request.model_id ?? submitModelId} for ${submitTargetLevel} review.`,
      );
      queryClient.invalidateQueries({ queryKey: ["quant-foundry", "promotion"] });
      setSubmitModelId("");
      setSubmitReviewNote("");
      window.setTimeout(() => setSubmitSuccess(null), 5_000);
    },
    onError: (err: unknown) => {
      setSubmitSuccess(null);
      setSubmitError(err instanceof ApiError ? err.message : String(err));
    },
  });

  return (
    <AppShell>
      <PageHeader
        title="Quant Foundry Promotion"
        description="Human-gated model promotion with evidence packets, confirmation dialogs, and completed receipts."
        action={<StatusPill intent={disabled ? "inactive" : "verified"} label={disabled ? "DISABLED" : "LIVE"} />}
      />

      <div className="grid gap-4 xl:grid-cols-[1.3fr_1fr]">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <ShieldCheck className="h-4 w-4 text-primary" />
              Pending review queue
            </CardTitle>
            <CardDescription>Approve or reject pending promotion requests with evidence confirmation.</CardDescription>
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

        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
                <Rocket className="h-4 w-4 text-primary" />
                Submit for promotion
              </CardTitle>
              <CardDescription>Submit a model for promotion review. Evidence is built from dossier + tournament + sentinel.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {disabled ? (
                <EmptyState title="Quant Foundry is disabled" body="Promotion submission is unavailable." />
              ) : (
                <>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">Model ID</label>
                    <Input
                      value={submitModelId}
                      onChange={(e) => setSubmitModelId(e.target.value)}
                      placeholder="e.g. gbm_predictor.v2"
                      disabled={submitMutation.isPending}
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">Target level</label>
                    <select
                      value={submitTargetLevel}
                      onChange={(e) => setSubmitTargetLevel(e.target.value)}
                      disabled={submitMutation.isPending}
                      className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {PROMOTION_LEVELS.map((level) => (
                        <option key={level} value={level}>{level}</option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs text-muted-foreground">Review note</label>
                    <textarea
                      value={submitReviewNote}
                      onChange={(e) => setSubmitReviewNote(e.target.value)}
                      placeholder="Operator review note..."
                      disabled={submitMutation.isPending}
                      rows={3}
                      className="flex w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                    />
                  </div>
                  <Button
                    onClick={() => submitMutation.mutate()}
                    disabled={submitMutation.isPending || !submitModelId.trim()}
                    className="w-full gap-2"
                  >
                    {submitMutation.isPending ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Rocket className="h-3.5 w-3.5" />
                    )}
                    {submitMutation.isPending ? "Submitting…" : "Submit for review"}
                  </Button>
                  {submitSuccess ? (
                    <div className="flex items-center gap-1.5 text-[11px] text-long">
                      <CheckCircle2 className="h-3 w-3" />
                      {submitSuccess}
                    </div>
                  ) : null}
                  {submitError ? (
                    <div className="flex items-center gap-1.5 text-[11px] text-destructive">
                      <AlertTriangle className="h-3 w-3" />
                      {submitError}
                    </div>
                  ) : null}
                </>
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
      </div>
    </AppShell>
  );
}

function PendingReviewCard({ entry }: { readonly entry: QuantFoundryPromotionQueueEntry }) {
  const token = useAuth((s) => s.token);
  const queryClient = useQueryClient();
  const issueCount = entry.evidence.blocking_issues.length;
  const [reviewNote, setReviewNote] = useState("");
  const [rejectionReason, setRejectionReason] = useState<string>(REJECTION_REASONS[1]);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const approveMutation = useMutation({
    mutationFn: () =>
      api.quantFoundryApprovePromotion(token, {
        model_id: entry.request.model_id,
        review_note: reviewNote || entry.request.review_note,
      }),
    onSuccess: (data) => {
      setErrorMsg(null);
      const decision = data.receipt?.decision ?? "unknown";
      setSuccessMsg(`Promotion ${decision} for ${entry.request.model_id}.`);
      queryClient.invalidateQueries({ queryKey: ["quant-foundry", "promotion"] });
      window.setTimeout(() => setSuccessMsg(null), 5_000);
    },
    onError: (err: unknown) => {
      setSuccessMsg(null);
      setErrorMsg(err instanceof ApiError ? err.message : String(err));
    },
  });

  const rejectMutation = useMutation({
    mutationFn: () =>
      api.quantFoundryRejectPromotion(token, {
        model_id: entry.request.model_id,
        review_note: reviewNote || entry.request.review_note,
        rejection_reason: rejectionReason,
      }),
    onSuccess: (data) => {
      setErrorMsg(null);
      const decision = data.receipt?.decision ?? "rejected";
      setSuccessMsg(`Promotion ${decision} for ${entry.request.model_id}.`);
      queryClient.invalidateQueries({ queryKey: ["quant-foundry", "promotion"] });
      window.setTimeout(() => setSuccessMsg(null), 5_000);
    },
    onError: (err: unknown) => {
      setSuccessMsg(null);
      setErrorMsg(err instanceof ApiError ? err.message : String(err));
    },
  });

  return (
    <div className="rounded-md border border-border/30 bg-card/40 p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <p className="font-mono text-sm text-foreground">{entry.request.model_id}</p>
          <p className="mt-1 text-xs text-muted-foreground">Target: {entry.request.target_level}. Evidence: {evidenceSummary(entry)}.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusPill intent={issueCount > 0 ? "critical" : "verified"} label={`${issueCount} BLOCKERS`} compact />
          <ReviewDialog
            entry={entry}
            decision="approve"
            reviewNote={reviewNote}
            setReviewNote={setReviewNote}
            rejectionReason={rejectionReason}
            setRejectionReason={setRejectionReason}
            isPending={approveMutation.isPending || rejectMutation.isPending}
            onApprove={() => approveMutation.mutate()}
            onReject={() => rejectMutation.mutate()}
          />
          <ReviewDialog
            entry={entry}
            decision="reject"
            reviewNote={reviewNote}
            setReviewNote={setReviewNote}
            rejectionReason={rejectionReason}
            setRejectionReason={setRejectionReason}
            isPending={approveMutation.isPending || rejectMutation.isPending}
            onApprove={() => approveMutation.mutate()}
            onReject={() => rejectMutation.mutate()}
          />
        </div>
      </div>
      <p className="mt-3 rounded-md border border-border/30 bg-background/40 p-2 text-xs text-muted-foreground">{entry.request.review_note}</p>
      {successMsg ? (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-long">
          <CheckCircle2 className="h-3 w-3" />
          {successMsg}
        </div>
      ) : null}
      {errorMsg ? (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-destructive">
          <AlertTriangle className="h-3 w-3" />
          {errorMsg}
        </div>
      ) : null}
    </div>
  );
}

function ReviewDialog({
  entry,
  decision,
  reviewNote,
  setReviewNote,
  rejectionReason,
  setRejectionReason,
  isPending,
  onApprove,
  onReject,
}: {
  readonly entry: QuantFoundryPromotionQueueEntry;
  readonly decision: "approve" | "reject";
  readonly reviewNote: string;
  readonly setReviewNote: (v: string) => void;
  readonly rejectionReason: string;
  readonly setRejectionReason: (v: string) => void;
  readonly isPending: boolean;
  readonly onApprove: () => void;
  readonly onReject: () => void;
}) {
  const isApprove = decision === "approve";
  return (
    <Dialog>
      <DialogTrigger className="rounded-md border border-border px-2 py-1 text-xs uppercase tracking-wide transition-colors hover:bg-muted/40">
        {decision}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 normal-case tracking-normal">
            {isApprove ? (
              <CheckCircle2 className="h-4 w-4 text-long" />
            ) : (
              <XCircle className="h-4 w-4 text-destructive" />
            )}
            {isApprove ? "Approve promotion" : "Reject promotion"}
          </DialogTitle>
          <DialogDescription>
            {isApprove
              ? "The gate will evaluate evidence and fail closed if requirements are not met."
              : "The promotion will be rejected with the selected reason."}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div className="space-y-1">
            <p><span className="text-muted-foreground">Model:</span> {entry.request.model_id}</p>
            <p><span className="text-muted-foreground">Target:</span> {entry.request.target_level}</p>
            <p><span className="text-muted-foreground">Evidence packet:</span> {evidenceSummary(entry)}</p>
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Review note</label>
            <textarea
              value={reviewNote}
              onChange={(e) => setReviewNote(e.target.value)}
              placeholder="Operator review note..."
              rows={3}
              className="flex w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
          {!isApprove ? (
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Rejection reason</label>
              <select
                value={rejectionReason}
                onChange={(e) => setRejectionReason(e.target.value)}
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {REJECTION_REASONS.map((reason) => (
                  <option key={reason} value={reason}>{reason}</option>
                ))}
              </select>
            </div>
          ) : null}
        </div>
        <div className="flex justify-end gap-2">
          {isPending ? (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Processing…
            </div>
          ) : (
            <>
              <Button
                variant={isApprove ? "default" : "destructive"}
                size="sm"
                onClick={isApprove ? onApprove : onReject}
                className="gap-2"
              >
                {isApprove ? (
                  <CheckCircle2 className="h-3.5 w-3.5" />
                ) : (
                  <XCircle className="h-3.5 w-3.5" />
                )}
                Confirm {decision}
              </Button>
              <DialogClose className="rounded-md border border-border px-3 py-2 text-sm hover:bg-muted/40">
                Cancel
              </DialogClose>
            </>
          )}
        </div>
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
      <p className="mt-1 text-muted-foreground">Target {review.request.target_level}. {review.rejection_reason ?? "Approved."}</p>
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
