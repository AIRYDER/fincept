"use client";

import {
  Activity,
  AlertTriangle,
  ExternalLink,
  History,
  Newspaper,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  NewsImpactHorizon,
  NewsImpactSignalEnvelope,
  NewsImpactSignalsResponse,
} from "@/lib/types";
import { cn, nsToDate } from "@/lib/utils";

export function ShadowNewsImpactPanel({
  response,
  isLoading,
  error,
  onRefresh,
  isRefreshing = false,
}: {
  response: NewsImpactSignalsResponse | null;
  isLoading: boolean;
  error?: unknown;
  onRefresh?: () => void;
  isRefreshing?: boolean;
}) {
  const signals = response?.signals ?? [];

  return (
    <Card className="border-cyan/25 bg-card/80">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 normal-case tracking-normal">
              <Newspaper className="h-4 w-4 text-cyan" />
              Shadow news-impact signals
            </CardTitle>
            <CardDescription>
              Recent `sig.news_impact` model outputs with analog evidence and
              point-in-time availability.
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="warn">Shadow only / not trade-driving</Badge>
            <Badge variant="muted">{response?.stream ?? "sig.news_impact"}</Badge>
            {onRefresh ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={onRefresh}
                disabled={isRefreshing}
              >
                <RefreshCw className={cn("h-3.5 w-3.5", isRefreshing && "animate-spin")} />
                Refresh
              </Button>
            ) : null}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <PanelNotice
            icon="error"
            title="Unable to load shadow signals"
            detail={error instanceof Error ? error.message : String(error)}
          />
        ) : isLoading ? (
          <PanelNotice
            icon="loading"
            title="Loading shadow news-impact signals"
            detail="Reading the recent Redis stream window from the API."
          />
        ) : signals.length === 0 ? (
          <PanelNotice
            icon="empty"
            title="No shadow news-impact signals yet"
            detail={`Waiting for ${response?.stream ?? "sig.news_impact"} events from the news impact agent.`}
          />
        ) : (
          <div className="space-y-3">
            {signals.map((item) => (
              <SignalCard key={item.stream_id} item={item} />
            ))}
          </div>
        )}
        <p className="text-[10px] leading-4 text-muted-foreground">
          Inspection surface only. These rows do not submit execution requests
          and do not drive portfolio changes.
        </p>
      </CardContent>
    </Card>
  );
}

function SignalCard({ item }: { item: NewsImpactSignalEnvelope }) {
  const signal = item.payload;
  const headline = signal.metadata.headline || signal.event_id;
  const source = signal.metadata.source || "unknown source";
  const available = formatNs(signal.available_at_ns);
  const horizonRows = Object.entries(signal.horizons);
  const primaryUrl = signal.source_urls[0] ?? null;

  return (
    <article className="border border-border/50 bg-background/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-cyan">{signal.symbol}</span>
            <Badge variant="outline">{signal.event_type}</Badge>
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
              {source}
            </span>
          </div>
          <h3 className="text-sm font-semibold leading-5">{headline}</h3>
          <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
            <span>Event {signal.event_id}</span>
            <span>Available {available}</span>
            <span>Stream {item.stream_id}</span>
          </div>
        </div>
        <div className="text-right">
          <div className="font-mono text-2xl text-primary">
            {formatWholePercent(signal.confidence)}
          </div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            confidence
          </div>
        </div>
      </div>

      <div className="mt-3 grid gap-3 xl:grid-cols-[1fr_0.85fr]">
        <div className="overflow-x-auto border border-border/40">
          <table className="w-full min-w-[640px] text-xs">
            <thead className="border-b border-border/40 bg-muted/20 text-[10px] uppercase tracking-widest text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Horizon</th>
                <th className="px-3 py-2 text-right">Expected</th>
                <th className="px-3 py-2 text-right">P(up)</th>
                <th className="px-3 py-2 text-right">Q10</th>
                <th className="px-3 py-2 text-right">Q50</th>
                <th className="px-3 py-2 text-right">Q90</th>
                <th className="px-3 py-2 text-right">Samples</th>
              </tr>
            </thead>
            <tbody>
              {horizonRows.map(([horizon, values]) => (
                <HorizonRow key={horizon} horizon={horizon} values={values} />
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-2">
          <div className="border border-border/40 bg-muted/10 p-3">
            <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
              <History className="h-3 w-3 text-cyan" />
              Similar events
            </div>
            {signal.similar_event_ids.length ? (
              <div className="flex flex-wrap gap-1.5">
                {signal.similar_event_ids.map((eventId) => (
                  <span
                    key={eventId}
                    className="border border-border/50 px-2 py-1 font-mono text-[10px]"
                  >
                    {eventId}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">No analog IDs attached.</p>
            )}
          </div>

          <div className="border border-border/40 bg-muted/10 p-3">
            <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground">
              <ShieldCheck className="h-3 w-3 text-cyan" />
              Model evidence
            </div>
            <div className="space-y-1 text-xs">
              <EvidenceLine label="Model" value={signal.model_version} />
              <EvidenceLine label="Agent" value={signal.agent_id} />
              {primaryUrl ? (
                <a
                  href={primaryUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex min-w-0 items-center justify-between gap-2 text-cyan hover:underline"
                >
                  <span className="truncate">{primaryUrl}</span>
                  <ExternalLink className="h-3 w-3 shrink-0" />
                </a>
              ) : (
                <EvidenceLine label="Source URL" value="not attached" muted />
              )}
            </div>
          </div>
        </div>
      </div>
    </article>
  );
}

function HorizonRow({
  horizon,
  values,
}: {
  horizon: string;
  values: NewsImpactHorizon;
}) {
  return (
    <tr className="border-b border-border/30 last:border-0">
      <td className="px-3 py-2 font-mono">{horizon}</td>
      <td className={cn("px-3 py-2 text-right font-mono", signedTone(values.expected_return))}>
        {formatReturn(values.expected_return)}
      </td>
      <td className="px-3 py-2 text-right font-mono">
        {formatWholePercent(values.p_up)}
      </td>
      <td className={cn("px-3 py-2 text-right font-mono", signedTone(values.q10))}>
        {formatReturn(values.q10)}
      </td>
      <td className={cn("px-3 py-2 text-right font-mono", signedTone(values.q50))}>
        {formatReturn(values.q50)}
      </td>
      <td className={cn("px-3 py-2 text-right font-mono", signedTone(values.q90))}>
        {formatReturn(values.q90)}
      </td>
      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
        {values.sample_size}
      </td>
    </tr>
  );
}

function EvidenceLine({
  label,
  value,
  muted = false,
}: {
  label: string;
  value: string;
  muted?: boolean;
}) {
  return (
    <div className="flex min-w-0 justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("truncate font-mono", muted && "text-muted-foreground")}>
        {value}
      </span>
    </div>
  );
}

function PanelNotice({
  icon,
  title,
  detail,
}: {
  icon: "loading" | "empty" | "error";
  title: string;
  detail: string;
}) {
  const Icon =
    icon === "error" ? AlertTriangle : icon === "loading" ? RefreshCw : Activity;
  return (
    <div className="flex min-h-[180px] flex-col items-center justify-center gap-3 border border-dashed border-border/60 bg-background/25 p-6 text-center">
      <Icon
        className={cn(
          "h-7 w-7",
          icon === "error" ? "text-short" : "text-cyan",
          icon === "loading" && "animate-spin",
        )}
      />
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="mt-1 max-w-xl text-xs leading-5 text-muted-foreground">
          {detail}
        </p>
      </div>
    </div>
  );
}

function formatReturn(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
}

function formatWholePercent(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

function signedTone(value: number): string {
  if (!Number.isFinite(value) || value === 0) return "text-muted-foreground";
  return value > 0 ? "text-long" : "text-short";
}

function formatNs(value: number): string {
  const date = nsToDate(value);
  if (!date) return "unknown";
  return date.toISOString();
}
