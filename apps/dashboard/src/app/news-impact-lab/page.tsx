"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  BrainCircuit,
  FlaskConical,
  Gauge,
  RefreshCw,
  SlidersHorizontal,
} from "lucide-react";
import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { KpiTile } from "@/components/widgets/kpi-tile";
import { PageHeader } from "@/components/widgets/page-header";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  NewsArticle,
  NewsImpactPredictBody,
  NewsImpactPrediction,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const HORIZONS = ["1m", "5m", "15m", "30m", "1h", "1d"];
const COLORS = ["#ff8a00", "#00e5ff", "#19c37d", "#f5c542", "#ff4d4f", "#8b5cf6"];

export default function NewsImpactLabPage() {
  const token = useAuth((s) => s.token);
  const [headline, setHeadline] = useState(
    "Amazon raises AWS outlook after AI infrastructure demand accelerates",
  );
  const [body, setBody] = useState("Management cited stronger AI workload demand and improving cloud margins.");
  const [symbols, setSymbols] = useState("AMZN");
  const [source, setSource] = useState("benzinga");
  const [eventType, setEventType] = useState("guidance");
  const [marketRegime, setMarketRegime] = useState("risk_on");
  const [topK, setTopK] = useState(5);
  const [selectedHorizons, setSelectedHorizons] = useState(["5m", "30m", "1h"]);
  const [selectedArticleId, setSelectedArticleId] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["news-impact-status"],
    queryFn: () => api.newsImpactStatus(token),
    enabled: !!token,
  });

  const news = useQuery({
    queryKey: ["news", "impact-lab"],
    queryFn: () => api.news(token, { limit: 80 }),
    enabled: !!token,
    refetchInterval: 15000,
  });

  const liveArticles = useMemo(
    () => [...(news.data?.alert ?? []), ...(news.data?.impact ?? []), ...(news.data?.universe ?? [])],
    [news.data],
  );

  const predictMutation = useMutation({
    mutationFn: (body: NewsImpactPredictBody) => api.newsImpactPredict(token, body),
  });

  const optimizeMutation = useMutation({
    mutationFn: () =>
      api.newsImpactOptimize(token, {
        horizon: selectedHorizons[0] ?? "5m",
        mode: "leave-one-out",
        top_k: topK,
      }),
  });

  const prediction = predictMutation.data?.prediction ?? null;
  const profile = status.data?.profile ?? predictMutation.data?.dataset_profile ?? null;

  function scoreManual() {
    predictMutation.mutate(buildBody());
  }

  function scoreArticle(article: NewsArticle) {
    const symbol = article.symbols.find((s) => s.in_book)?.symbol ?? article.symbols[0]?.symbol ?? "";
    setSelectedArticleId(article.id);
    setHeadline(article.headline);
    setBody(article.summary ?? "");
    setSource(article.source || "news");
    setSymbols(article.symbols.map((s) => s.symbol).join(", "));
    setEventType(inferEventType(article.headline));
    predictMutation.mutate(
      buildBody({
        event_id: article.id,
        source: article.source || "news",
        headline: article.headline,
        body: article.summary ?? "",
        symbols: article.symbols.map((s) => s.symbol),
        event_type: inferEventType(article.headline),
        available_at_ns: article.ts_event_ns,
        symbol,
      }),
    );
  }

  return (
    <AppShell>
      <PageHeader
        title="News Impact Lab"
        description="Experimental analog model workbench. This tests predicted market impact from historical similar events without changing the production News page."
        action={
          <div className="flex items-center gap-2">
            <Badge variant="warn">Experiment</Badge>
            <Button variant="outline" size="sm" onClick={() => news.refetch()}>
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh news
            </Button>
          </div>
        }
      />

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <KpiTile
          label="Historical outcomes"
          value={profile ? String(profile.event_count) : "—"}
          icon={FlaskConical}
          sub="sample analog corpus"
        />
        <KpiTile
          label="Horizons"
          value={profile ? String(profile.horizons.length) : "—"}
          icon={Activity}
          sub={profile?.horizons.join(" · ") ?? "loading"}
        />
        <KpiTile
          label="Confidence"
          value={prediction ? `${(prediction.confidence * 100).toFixed(0)}%` : "—"}
          icon={Gauge}
          sub={prediction?.model_version ?? "no prediction yet"}
        />
        <KpiTile
          label="Similar events"
          value={prediction ? String(prediction.similar_events.length) : "—"}
          icon={BrainCircuit}
          sub="top analog matches"
        />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[430px_1fr]">
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>
                <SlidersHorizontal className="h-3.5 w-3.5 text-primary" />
                Manual event tester
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <Field label="Headline">
                <textarea
                  value={headline}
                  onChange={(e) => setHeadline(e.target.value)}
                  className="min-h-20 w-full border border-border bg-background p-2 text-sm text-foreground"
                />
              </Field>
              <Field label="Body / context">
                <textarea
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  className="min-h-24 w-full border border-border bg-background p-2 text-sm text-foreground"
                />
              </Field>
              <div className="grid gap-2 sm:grid-cols-2">
                <Field label="Symbols">
                  <Input value={symbols} onChange={(e) => setSymbols(e.target.value.toUpperCase())} />
                </Field>
                <Field label="Source">
                  <Input value={source} onChange={(e) => setSource(e.target.value)} />
                </Field>
                <Field label="Event type">
                  <Input value={eventType} onChange={(e) => setEventType(e.target.value)} />
                </Field>
                <Field label="Regime">
                  <select
                    value={marketRegime}
                    onChange={(e) => setMarketRegime(e.target.value)}
                    className="h-9 w-full border border-border bg-background px-2 text-sm"
                  >
                    <option value="unknown">unknown</option>
                    <option value="risk_on">risk_on</option>
                    <option value="risk_off">risk_off</option>
                    <option value="high_vol">high_vol</option>
                  </select>
                </Field>
              </div>
              <Field label="Horizons">
                <div className="flex flex-wrap gap-2">
                  {HORIZONS.map((horizon) => {
                    const active = selectedHorizons.includes(horizon);
                    return (
                      <button
                        key={horizon}
                        onClick={() =>
                          setSelectedHorizons((current) =>
                            active
                              ? current.filter((h) => h !== horizon)
                              : [...current, horizon],
                          )
                        }
                        className={cn(
                          "border px-2 py-1 text-[10px] uppercase tracking-wider",
                          active
                            ? "border-primary/70 bg-primary/15 text-primary"
                            : "border-border text-muted-foreground",
                        )}
                      >
                        {horizon}
                      </button>
                    );
                  })}
                </div>
              </Field>
              <Field label="Top analogs">
                <Input
                  type="number"
                  min={1}
                  max={20}
                  value={topK}
                  onChange={(e) => setTopK(Number(e.target.value))}
                />
              </Field>
              <div className="flex flex-wrap gap-2">
                <Button onClick={scoreManual} disabled={predictMutation.isPending || !headline.trim()}>
                  <BrainCircuit className="h-3.5 w-3.5" />
                  Score event
                </Button>
                <Button
                  variant="outline"
                  onClick={() => optimizeMutation.mutate()}
                  disabled={optimizeMutation.isPending}
                >
                  <BarChart3 className="h-3.5 w-3.5" />
                  Optimize weights
                </Button>
              </div>
              {predictMutation.error ? (
                <p className="text-xs text-short">{String(predictMutation.error)}</p>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Live news queue</CardTitle>
            </CardHeader>
            <CardContent className="max-h-[520px] space-y-2 overflow-y-auto">
              {liveArticles.slice(0, 35).map((article) => (
                <button
                  key={article.id}
                  onClick={() => scoreArticle(article)}
                  className={cn(
                    "block w-full border p-2 text-left transition-colors hover:border-primary/60",
                    selectedArticleId === article.id ? "border-primary/70 bg-primary/10" : "border-border",
                  )}
                >
                  <div className="mb-1 flex items-center justify-between gap-3 text-[10px] uppercase tracking-wider text-muted-foreground">
                    <span>{article.source || "news"}</span>
                    <span>{article.symbols.map((s) => s.symbol).slice(0, 4).join(" · ")}</span>
                  </div>
                  <div className="text-sm leading-5 text-foreground">{article.headline}</div>
                </button>
              ))}
              {!liveArticles.length ? (
                <p className="text-sm text-muted-foreground">No current news rows loaded.</p>
              ) : null}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          {prediction ? <PredictionPanel prediction={prediction} /> : <EmptyPrediction />}
          {optimizeMutation.data ? (
            <OptimizationPanel data={optimizeMutation.data.optimization} />
          ) : null}
          {profile ? <DatasetPanel profile={profile} /> : null}
        </div>
      </div>
    </AppShell>
  );

  function buildBody(overrides: Partial<NewsImpactPredictBody["event"]> & { symbol?: string } = {}): NewsImpactPredictBody {
    const tickerList = (overrides.symbols ?? symbols.split(/[,\s|;]+/))
      .map((s) => String(s).trim().toUpperCase())
      .filter(Boolean);
    const symbol = overrides.symbol ?? tickerList[0] ?? "";
    return {
      event: {
        event_id: overrides.event_id ?? "dashboard-manual",
        source: overrides.source ?? source,
        headline: overrides.headline ?? headline,
        body: overrides.body ?? body,
        symbols: tickerList,
        event_type: overrides.event_type ?? eventType,
        language: "en",
        available_at_ns: overrides.available_at_ns,
      },
      context: {
        symbol,
        market_regime: marketRegime,
        relative_volume: 1,
      },
      horizons: selectedHorizons.length ? selectedHorizons : ["5m", "30m", "1h"],
      top_k: topK,
    };
  }
}

function PredictionPanel({ prediction }: { prediction: NewsImpactPrediction }) {
  const horizonRows = Object.entries(prediction.horizons).map(([horizon, impact]) => ({
    horizon,
    expected: impact.expected_return * 100,
    q10: impact.q10 * 100,
    q50: impact.q50 * 100,
    q90: impact.q90 * 100,
    pUp: impact.p_up * 100,
  }));

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Activity className="h-3.5 w-3.5 text-primary" />
          Predicted market impact · {prediction.symbol}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-3">
          <MiniStat label="Event type" value={prediction.event_type} />
          <MiniStat label="Vol impact" value={`${(prediction.volatility_impact * 100).toFixed(1)}%`} />
          <MiniStat label="Volume impact" value={`${(prediction.volume_impact * 100).toFixed(1)}%`} />
        </div>
        <div className="h-72 border border-border p-2">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={horizonRows}>
              <CartesianGrid stroke="#242424" vertical={false} />
              <XAxis dataKey="horizon" tick={{ fill: "#888", fontSize: 11 }} />
              <YAxis tick={{ fill: "#888", fontSize: 11 }} />
              <Tooltip contentStyle={{ background: "#050505", border: "1px solid #242424" }} />
              <Bar dataKey="expected" name="Expected return %" fill="#ff8a00" radius={[2, 2, 0, 0]} />
              <Bar dataKey="pUp" name="P(up) %" fill="#00e5ff" radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="overflow-x-auto border border-border">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr className="border-b border-border">
                <th className="px-3 py-2 text-left">Horizon</th>
                <th className="px-3 py-2 text-right">Expected</th>
                <th className="px-3 py-2 text-right">P(up)</th>
                <th className="px-3 py-2 text-right">Q10</th>
                <th className="px-3 py-2 text-right">Q50</th>
                <th className="px-3 py-2 text-right">Q90</th>
              </tr>
            </thead>
            <tbody>
              {horizonRows.map((row) => (
                <tr key={row.horizon} className="border-b border-border/40 last:border-0">
                  <td className="px-3 py-2 font-mono">{row.horizon}</td>
                  <td className={cn("px-3 py-2 text-right font-mono", row.expected >= 0 ? "text-long" : "text-short")}>{row.expected.toFixed(2)}%</td>
                  <td className="px-3 py-2 text-right font-mono">{row.pUp.toFixed(1)}%</td>
                  <td className="px-3 py-2 text-right font-mono">{row.q10.toFixed(2)}%</td>
                  <td className="px-3 py-2 text-right font-mono">{row.q50.toFixed(2)}%</td>
                  <td className="px-3 py-2 text-right font-mono">{row.q90.toFixed(2)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <section className="grid gap-3 md:grid-cols-2">
          {prediction.similar_events.map((event, index) => (
            <article key={event.event_id} className="border border-border p-3">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  Analog {index + 1} · {event.source}
                </span>
                <span className="font-mono text-xs text-primary">{event.score.toFixed(3)}</span>
              </div>
              <p className="text-sm leading-5 text-foreground">{event.headline}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {Object.entries(event.abnormal_returns).map(([horizon, value], i) => (
                  <span key={horizon} className="border border-border px-1.5 py-0.5 text-[10px]">
                    <Cell fill={COLORS[i % COLORS.length]} />
                    {horizon}: {(value * 100).toFixed(2)}%
                  </span>
                ))}
              </div>
            </article>
          ))}
        </section>
      </CardContent>
    </Card>
  );
}

function OptimizationPanel({ data }: { data: any }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Optimized analog weights</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 md:grid-cols-3">
          <MiniStat label="Horizon" value={data.horizon} />
          <MiniStat label="MAE" value={data.metrics?.mae == null ? "n/a" : (data.metrics.mae * 100).toFixed(2) + "%"} />
          <MiniStat label="Direction hit" value={data.metrics?.directional_accuracy == null ? "n/a" : (data.metrics.directional_accuracy * 100).toFixed(0) + "%"} />
        </div>
        <div className="grid gap-2 md:grid-cols-2">
          {Object.entries(data.weights ?? {}).map(([key, value]) => (
            <div key={key} className="flex items-center justify-between border border-border px-3 py-2 text-xs">
              <span className="uppercase tracking-wider text-muted-foreground">{key}</span>
              <span className="font-mono text-foreground">{Number(value).toFixed(2)}</span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function DatasetPanel({ profile }: { profile: any }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Dataset profile</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 md:grid-cols-3">
        <MiniStat label="Sources" value={Object.keys(profile.sources ?? {}).length} />
        <MiniStat label="Event types" value={Object.keys(profile.event_types ?? {}).length} />
        <MiniStat label="Symbols" value={Object.keys(profile.symbols ?? {}).length} />
      </CardContent>
    </Card>
  );
}

function EmptyPrediction() {
  return (
    <Card>
      <CardContent className="flex min-h-[520px] flex-col items-center justify-center gap-3 border border-dashed border-border text-center">
        <BrainCircuit className="h-8 w-8 text-primary" />
        <h2 className="text-lg font-semibold">Score a story to inspect impact analogs</h2>
        <p className="max-w-xl text-sm leading-6 text-muted-foreground">
          Pick a live headline or submit a manual event. The lab compares it against historical outcomes and estimates multi-horizon abnormal return distributions.
        </p>
      </CardContent>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function MiniStat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="border border-border p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-1 truncate font-mono text-lg text-foreground">{value}</div>
    </div>
  );
}

function inferEventType(headline: string): string {
  const h = headline.toLowerCase();
  if (h.includes("price target") || h.includes("upgrade") || h.includes("downgrade")) return "analyst";
  if (h.includes("earnings") || h.includes("q1") || h.includes("q2") || h.includes("q3") || h.includes("q4")) return "earnings";
  if (h.includes("fda") || h.includes("approval")) return "regulatory";
  if (h.includes("guidance") || h.includes("outlook")) return "guidance";
  if (h.includes("merger") || h.includes("acquisition")) return "m&a";
  return "general";
}
