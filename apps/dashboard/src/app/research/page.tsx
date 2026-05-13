"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpRight,
  BadgeCheck,
  BrainCircuit,
  DatabaseZap,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
} from "lucide-react";
import { useMemo, useState } from "react";

import { EvidenceStack, type EvidenceRow } from "@/components/evidence/evidence-stack";
import { AppShell } from "@/components/shell/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/widgets/page-header";
import { ApiError, api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  ExaResearchResponse,
  ExaSearchType,
  OpenBBCallResponse,
  OpenBBHealthResponse,
  OpenBBQuoteResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const SEARCH_TYPES: ExaSearchType[] = ["auto", "fast", "deep", "deep-reasoning"];
const FRESHNESS = [
  { label: "Default", value: null },
  { label: "24h", value: 24 },
  { label: "Live", value: 0 },
] as const;
const OPENBB_DISPATCH_PRESETS = [
  {
    label: "Income",
    path: "/api/v1/equity/fundamental/income",
    description: "Revenue / net income statement rows",
  },
  {
    label: "Balance",
    path: "/api/v1/equity/fundamental/balance",
    description: "Assets, liabilities, and equity",
  },
  {
    label: "Cash flow",
    path: "/api/v1/equity/fundamental/cash",
    description: "Operating, investing, financing cash flow",
  },
] as const;

export default function ResearchPage() {
  const token = useAuth((s) => s.token);
  const [query, setQuery] = useState("NVDA Blackwell supply constraints next two quarters");
  const [symbol, setSymbol] = useState("NVDA");
  const [quoteSymbol, setQuoteSymbol] = useState("NVDA");
  const [quoteProvider, setQuoteProvider] = useState("yfinance");
  const [fundSymbol, setFundSymbol] = useState("NVDA");
  const [fundProvider, setFundProvider] = useState("yfinance");
  const [fundPath, setFundPath] = useState<(typeof OPENBB_DISPATCH_PRESETS)[number]["path"]>(
    OPENBB_DISPATCH_PRESETS[0].path,
  );
  const [searchType, setSearchType] = useState<ExaSearchType>("deep");
  const [freshness, setFreshness] = useState<number | null>(24);

  const exaMutation = useMutation({
    mutationFn: () =>
      api.exaResearch(token, {
        query,
        symbol: symbol.trim() || null,
        search_type: searchType,
        max_age_hours: freshness,
        num_results: 10,
      }),
  });
  const openbbMutation = useMutation({
    mutationFn: () =>
      api.openbbQuote(token, {
        symbol: quoteSymbol.trim().toUpperCase(),
        provider: quoteProvider.trim() || "yfinance",
      }),
  });
  const fundamentalsMutation = useMutation({
    mutationFn: () =>
      api.openbbCall(token, {
        path: fundPath,
        params: {
          symbol: fundSymbol.trim().toUpperCase(),
          provider: fundProvider.trim() || "yfinance",
          period: "annual",
          limit: "4",
        },
      }),
  });
  const openbbHealthQuery = useQuery({
    queryKey: ["openbb", "health"],
    queryFn: () => api.openbbHealth(token),
    enabled: !!token,
    refetchInterval: 30_000,
    staleTime: 15_000,
    placeholderData: (prev) => prev,
  });

  const result = exaMutation.data ?? null;
  const quoteResult = openbbMutation.data ?? null;
  const fundamentalsResult = fundamentalsMutation.data ?? null;
  const openbbHealth = openbbHealthQuery.data ?? null;
  const canSearch = !!token && query.trim().length >= 3 && !exaMutation.isPending;
  const canQuote = !!token && quoteSymbol.trim().length >= 1 && !openbbMutation.isPending;
  const canFetchFundamentals =
    !!token && fundSymbol.trim().length >= 1 && !fundamentalsMutation.isPending;

  function submit() {
    if (canSearch) exaMutation.mutate();
  }

  return (
    <AppShell>
      <div className="space-y-2">
        <PageHeader
          title="Research"
          description="Exa briefs and OpenBB market data with structured evidence, risks, catalysts, and source grounding."
          action={
            <div className="flex items-center gap-2">
              <Badge variant="outline">Read-only</Badge>
              <Badge variant="secondary">No order path</Badge>
            </div>
          }
        />

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Search className="h-4 w-4 text-primary" />
              Research query
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-2 lg:grid-cols-[140px_1fr_auto]">
              <Input
                value={symbol}
                onChange={(event) => setSymbol(event.target.value.toUpperCase())}
                placeholder="SYMBOL"
                className="uppercase"
              />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") submit();
                }}
                placeholder="Ask for catalysts, risks, filings, product launches, macro context..."
              />
              <Button onClick={submit} disabled={!canSearch}>
                {exaMutation.isPending ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Sparkles className="h-4 w-4" />
                )}
                Search Exa
              </Button>
            </div>

            <div className="flex flex-wrap gap-2">
              {SEARCH_TYPES.map((type) => (
                <button
                  key={type}
                  onClick={() => setSearchType(type)}
                  className={cn(
                    "h-7 border border-border px-3 text-[10px] font-semibold uppercase text-muted-foreground transition-colors",
                    searchType === type && "border-primary bg-primary/10 text-primary",
                  )}
                >
                  {type}
                </button>
              ))}
              <div className="mx-1 h-7 w-px bg-border" />
              {FRESHNESS.map((item) => (
                <button
                  key={item.label}
                  onClick={() => setFreshness(item.value)}
                  className={cn(
                    "h-7 border border-border px-3 text-[10px] font-semibold uppercase text-muted-foreground transition-colors",
                    freshness === item.value && "border-primary bg-primary/10 text-primary",
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <DatabaseZap className="h-4 w-4 text-primary" />
                OpenBB quote
              </CardTitle>
              <OpenBBStatusBadge
                token={token}
                health={openbbHealth}
                isLoading={!!token && openbbHealthQuery.isFetching && !openbbHealth}
              />
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-2 lg:grid-cols-[140px_180px_auto_1fr]">
              <Input
                value={quoteSymbol}
                onChange={(event) => setQuoteSymbol(event.target.value.toUpperCase())}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && canQuote) openbbMutation.mutate();
                }}
                placeholder="SYMBOL"
                className="uppercase"
              />
              <Input
                value={quoteProvider}
                onChange={(event) => setQuoteProvider(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && canQuote) openbbMutation.mutate();
                }}
                placeholder="provider"
              />
              <Button onClick={() => openbbMutation.mutate()} disabled={!canQuote} variant="outline">
                {openbbMutation.isPending ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <DatabaseZap className="h-4 w-4" />
                )}
                Quote
              </Button>
              <div className="flex min-h-10 items-center text-xs text-muted-foreground">
                Uses the backend OpenBB adapter. Install OpenBB to enable live rows.
              </div>
            </div>

            {openbbHealth && !openbbHealth.ok ? (
              <div className="flex items-center gap-2 rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                <ShieldAlert className="h-3.5 w-3.5" />
                <span>{openbbHealth.error ?? openbbHealth.error_type ?? "OpenBB API unreachable"}</span>
              </div>
            ) : null}

            {openbbMutation.error ? (
              <ResearchApiError error={openbbMutation.error} />
            ) : null}

            {quoteResult ? <OpenBBQuote result={quoteResult} /> : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <DatabaseZap className="h-4 w-4 text-cyan" />
              OpenBB dispatcher proof
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-2 lg:grid-cols-[140px_180px_auto_1fr]">
              <Input
                value={fundSymbol}
                onChange={(event) => setFundSymbol(event.target.value.toUpperCase())}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && canFetchFundamentals) {
                    fundamentalsMutation.mutate();
                  }
                }}
                placeholder="SYMBOL"
                className="uppercase"
              />
              <Input
                value={fundProvider}
                onChange={(event) => setFundProvider(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && canFetchFundamentals) {
                    fundamentalsMutation.mutate();
                  }
                }}
                placeholder="provider"
              />
              <Button
                onClick={() => fundamentalsMutation.mutate()}
                disabled={!canFetchFundamentals}
                variant="outline"
              >
                {fundamentalsMutation.isPending ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <DatabaseZap className="h-4 w-4" />
                )}
                Fetch
              </Button>
              <div className="flex min-h-10 items-center text-xs text-muted-foreground">
                Calls <code className="mx-1 text-cyan">POST /research/openbb</code> with a generic OpenBB path.
              </div>
            </div>

            <div className="flex flex-wrap gap-2">
              {OPENBB_DISPATCH_PRESETS.map((preset) => (
                <button
                  key={preset.path}
                  type="button"
                  onClick={() => setFundPath(preset.path)}
                  title={preset.description}
                  className={cn(
                    "border border-border px-3 py-1.5 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground transition-colors",
                    fundPath === preset.path && "border-cyan/60 bg-cyan/10 text-cyan",
                  )}
                >
                  {preset.label}
                </button>
              ))}
              <span className="flex items-center font-mono text-[10px] text-muted-foreground">
                {fundPath}
              </span>
            </div>

            {fundamentalsMutation.error ? (
              <ResearchApiError error={fundamentalsMutation.error} />
            ) : null}

            {fundamentalsResult ? <OpenBBCallResult result={fundamentalsResult} /> : null}
          </CardContent>
        </Card>

        {exaMutation.error ? (
          <Card className="border-destructive/50">
            <CardContent className="flex items-center gap-3 py-4 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" />
              <ResearchApiErrorBody error={exaMutation.error} />
            </CardContent>
          </Card>
        ) : null}

        {result ? <ResearchBrief result={result} /> : <EmptyResearchState />}
      </div>
    </AppShell>
  );
}

function OpenBBQuote({ result }: { result: OpenBBQuoteResponse }) {
  if (!result.ok) {
    return (
      <div className="flex items-center gap-3 border border-destructive/50 px-3 py-2 text-sm text-destructive">
        <ShieldAlert className="h-4 w-4" />
        {result.error ?? result.error_type ?? "OpenBB quote failed"}
      </div>
    );
  }

  const first = result.results[0] ?? {};
  const rows = Object.entries(first).filter(([, value]) => value !== null && value !== undefined);

  return (
    <div className="space-y-2">
      <EvidenceStack
        title="OpenBB quote evidence"
        summary={`${result.provider} returned ${result.results.length} quote row${result.results.length === 1 ? "" : "s"} with ${rows.length} populated field${rows.length === 1 ? "" : "s"}.`}
        evidence={openbbQuoteEvidenceRows(result, rows.length)}
        payload={result}
        trace={openbbTraceRows(first)}
        tone={result.results.length ? "verified" : "caveat"}
      />
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {rows.length ? (
          rows.slice(0, 12).map(([key, value]) => (
            <div key={key} className="border border-border bg-muted/20 p-3">
              <div className="text-[10px] font-semibold uppercase text-muted-foreground">{key}</div>
              <div className="mt-1 truncate text-sm font-semibold">{String(value)}</div>
            </div>
          ))
        ) : (
          <p className="text-sm text-muted-foreground">No quote rows returned from {result.provider}.</p>
        )}
      </div>
    </div>
  );
}

function OpenBBCallResult({ result }: { result: OpenBBCallResponse }) {
  if (!result.ok) {
    return (
      <div className="flex items-center gap-3 border border-destructive/50 px-3 py-2 text-sm text-destructive">
        <ShieldAlert className="h-4 w-4" />
        {result.error ?? result.error_type ?? "OpenBB dispatcher call failed"}
      </div>
    );
  }

  const rows = result.results.slice(0, 4);
  const keys = Array.from(
    new Set(rows.flatMap((row) => Object.keys(row).slice(0, 8))),
  ).slice(0, 8);

  return (
    <div className="space-y-2">
      <EvidenceStack
        title="OpenBB dispatcher evidence"
        summary={`${result.path} returned ${result.results.length} row${result.results.length === 1 ? "" : "s"}${result.provider ? ` from ${result.provider}` : ""}.`}
        evidence={openbbCallEvidenceRows(result, keys.length)}
        payload={result}
        trace={result.results.slice(0, 4).map((row, index) => ({
          label: `row ${index + 1}`,
          value: Object.entries(row)
            .slice(0, 6)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join(" · "),
          tone: "verified",
        }))}
        tone={result.results.length ? "verified" : "caveat"}
      />
      <div className="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-widest text-muted-foreground">
        <span className="font-mono text-cyan">{result.path}</span>
        {result.provider ? <span>Provider: {result.provider}</span> : null}
        <span>{result.results.length} row{result.results.length === 1 ? "" : "s"}</span>
      </div>
      {rows.length ? (
        <div className="overflow-x-auto border border-border/60">
          <table className="w-full min-w-[720px] text-xs">
            <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr>
                {keys.map((key) => (
                  <th key={key} className="px-3 py-2 text-left">
                    {key}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => (
                <tr key={idx} className="border-t border-border/40">
                  {keys.map((key) => (
                    <td key={key} className="max-w-[220px] truncate px-3 py-2 font-mono">
                      {row[key] === null || row[key] === undefined
                        ? "—"
                        : String(row[key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No rows returned from dispatcher.</p>
      )}
    </div>
  );
}

function OpenBBStatusBadge({
  token,
  health,
  isLoading,
}: {
  token: string | null;
  health: OpenBBHealthResponse | null;
  isLoading: boolean;
}) {
  let state: "unauth" | "checking" | "ok" | "warn" | "error" = "checking";
  let label = "Checking";

  if (!token) {
    state = "unauth";
    label = "Sign in to probe";
  } else if (health?.ok) {
    state = health.warning ? "warn" : "ok";
    label = health.latency_ms ? `Online · ${health.latency_ms}ms` : "Online";
  } else if (health && !health.ok) {
    state = "error";
    label = health.error_type ?? "Offline";
  } else if (isLoading) {
    state = "checking";
    label = "Checking";
  }

  const className = cn(
    "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-widest",
    state === "ok" && "border-emerald-500/40 bg-emerald-500/10 text-emerald-500",
    state === "warn" && "border-amber-500/40 bg-amber-500/10 text-amber-600",
    state === "error" && "border-destructive/50 bg-destructive/10 text-destructive",
    state === "checking" && "border-border bg-background/40 text-muted-foreground",
    state === "unauth" && "border-border bg-background/40 text-muted-foreground",
  );

  return (
    <div className={className}>
      <span
        className={cn("h-2 w-2 rounded-full", {
          "bg-emerald-500": state === "ok",
          "bg-amber-500": state === "warn",
          "bg-destructive": state === "error",
          "bg-muted-foreground/50": state === "checking" || state === "unauth",
        })}
      />
      {label}
    </div>
  );
}

function ResearchBrief({ result }: { result: ExaResearchResponse }) {
  const citationCount = useMemo(
    () => result.grounding.reduce((total, item) => total + item.citations.length, 0),
    [result.grounding],
  );

  if (!result.ok) {
    return (
      <Card className="border-destructive/50">
        <CardContent className="flex items-center gap-3 py-4 text-sm text-destructive">
          <ShieldAlert className="h-4 w-4" />
          {result.error ?? result.error_type ?? "Research tool failed"}
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid gap-2 xl:grid-cols-[1.3fr_0.9fr]">
      <div className="space-y-2">
        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-base">{result.brief.headline}</CardTitle>
              <div className="flex items-center gap-2 text-[10px] uppercase text-muted-foreground">
                <span>{citationCount} citations</span>
                {result.cost_dollars !== null && result.cost_dollars !== undefined ? (
                  <span>${result.cost_dollars.toFixed(4)}</span>
                ) : null}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <p className="max-w-5xl text-sm leading-6 text-muted-foreground">
              {result.brief.summary}
            </p>
          </CardContent>
        </Card>

        <EvidenceStack
          title="Exa research evidence"
          summary={result.brief.summary}
          evidence={exaEvidenceRows(result, citationCount)}
          payload={{
            brief: result.brief,
            grounding: result.grounding,
            sources: result.sources,
            request_id: result.request_id,
            cost_dollars: result.cost_dollars,
          }}
          trace={exaTraceRows(result)}
          tone={citationCount ? "verified" : "caveat"}
        />

        <div className="grid gap-2 lg:grid-cols-2">
          <ListCard title="Bull case" tone="long" items={result.brief.bull_case} />
          <ListCard title="Bear case" tone="short" items={result.brief.bear_case} />
        </div>

        <div className="grid gap-2 lg:grid-cols-3">
          <ListCard title="Catalysts" items={result.brief.catalysts} />
          <ListCard title="Risks" tone="warn" items={result.brief.risks} />
          <ListCard title="Watch items" items={result.brief.watch_items} />
        </div>
      </div>

      <div className="space-y-2">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <BadgeCheck className="h-4 w-4 text-primary" />
              Grounding
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {result.grounding.length ? (
              result.grounding.map((item) => (
                <div key={item.field} className="border border-border bg-muted/20 p-3">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold uppercase">{item.field}</span>
                    <Badge variant={item.confidence === "high" ? "default" : "outline"}>
                      {item.confidence ?? "unknown"}
                    </Badge>
                  </div>
                  <div className="space-y-1">
                    {item.citations.map((citation) => (
                      <SourceLink key={`${item.field}-${citation.url}`} citation={citation} />
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">No field-level grounding returned.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">Sources</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1">
            {result.sources.map((source) => (
              <SourceLink key={source.url} citation={source} />
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function exaEvidenceRows(result: ExaResearchResponse, citationCount: number): EvidenceRow[] {
  const highConfidence = result.grounding.filter((item) => item.confidence === "high").length;
  return [
    {
      label: "citations",
      value: `${citationCount} citation${citationCount === 1 ? "" : "s"}`,
      tone: citationCount ? "verified" : "caveat",
    },
    {
      label: "sources",
      value: `${result.sources.length} source${result.sources.length === 1 ? "" : "s"}`,
      tone: result.sources.length ? "verified" : "caveat",
    },
    {
      label: "grounding fields",
      value: `${result.grounding.length} field${result.grounding.length === 1 ? "" : "s"}`,
      tone: result.grounding.length ? "verified" : "caveat",
    },
    {
      label: "high confidence",
      value: `${highConfidence}/${result.grounding.length}`,
      tone: highConfidence ? "verified" : "muted",
    },
    {
      label: "request",
      value: result.request_id ?? "not returned",
      tone: result.request_id ? "verified" : "muted",
    },
    {
      label: "cost",
      value:
        result.cost_dollars !== null && result.cost_dollars !== undefined
          ? `$${result.cost_dollars.toFixed(4)}`
          : "not returned",
      tone: result.cost_dollars !== null && result.cost_dollars !== undefined ? "verified" : "muted",
    },
  ];
}

function exaTraceRows(result: ExaResearchResponse): EvidenceRow[] {
  return result.grounding.map((item) => ({
    label: item.field,
    value:
      item.citations.map((citation) => citation.title || citation.url).join(" · ") ||
      "no citations",
    tone:
      item.confidence === "high"
        ? "verified"
        : item.confidence === "low"
          ? "caveat"
          : "muted",
  }));
}

function openbbQuoteEvidenceRows(result: OpenBBQuoteResponse, populatedFieldCount: number): EvidenceRow[] {
  return [
    { label: "provider", value: result.provider, tone: "verified" },
    {
      label: "rows",
      value: `${result.results.length}`,
      tone: result.results.length ? "verified" : "caveat",
    },
    {
      label: "populated fields",
      value: `${populatedFieldCount}`,
      tone: populatedFieldCount ? "verified" : "caveat",
    },
  ];
}

function openbbCallEvidenceRows(result: OpenBBCallResponse, visibleColumnCount: number): EvidenceRow[] {
  return [
    { label: "path", value: result.path, tone: "verified" },
    {
      label: "provider",
      value: result.provider ?? "not returned",
      tone: result.provider ? "verified" : "muted",
    },
    {
      label: "rows",
      value: `${result.results.length}`,
      tone: result.results.length ? "verified" : "caveat",
    },
    {
      label: "visible columns",
      value: `${visibleColumnCount}`,
      tone: visibleColumnCount ? "verified" : "caveat",
    },
  ];
}

function openbbTraceRows(row: Record<string, unknown>): EvidenceRow[] {
  return Object.entries(row)
    .filter(([, value]) => value !== null && value !== undefined)
    .slice(0, 8)
    .map(([key, value]) => ({
      label: key,
      value: String(value),
      tone: "verified",
    }));
}

function ListCard({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone?: "long" | "short" | "warn";
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle
          className={cn(
            "text-sm",
            tone === "long" && "text-long",
            tone === "short" && "text-short",
            tone === "warn" && "text-warn",
          )}
        >
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {items.length ? (
            items.map((item) => (
              <li key={item} className="border-l border-border pl-3 text-sm leading-5 text-muted-foreground">
                {item}
              </li>
            ))
          ) : (
            <li className="text-sm text-muted-foreground">No items returned.</li>
          )}
        </ul>
      </CardContent>
    </Card>
  );
}

function SourceLink({ citation }: { citation: { url: string; title?: string | null } }) {
  return (
    <a
      href={citation.url}
      target="_blank"
      rel="noreferrer"
      className="flex items-center justify-between gap-2 border border-border bg-background/50 px-2 py-1.5 text-xs text-muted-foreground hover:border-primary hover:text-foreground"
    >
      <span className="truncate">{citation.title || citation.url}</span>
      <ArrowUpRight className="h-3 w-3 shrink-0" />
    </a>
  );
}

function ResearchApiError({ error }: { error: unknown }) {
  return (
    <div className="flex items-start gap-3 border border-destructive/50 px-3 py-2 text-sm text-destructive">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <ResearchApiErrorBody error={error} />
    </div>
  );
}

function ResearchApiErrorBody({ error }: { error: unknown }) {
  const details = apiErrorDetails(error);
  return (
    <div className="min-w-0 space-y-1">
      <div>{details.message}</div>
      {details.meta.length ? (
        <div className="flex flex-wrap gap-1 text-[10px] uppercase tracking-widest text-destructive/80">
          {details.meta.map((item) => (
            <span key={item} className="border border-destructive/30 px-1.5 py-0.5">
              {item}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function apiErrorDetails(error: unknown): { message: string; meta: string[] } {
  if (!(error instanceof ApiError)) {
    return { message: error instanceof Error ? error.message : String(error), meta: [] };
  }
  const meta = [`HTTP ${error.status}`];
  const body = error.body;
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const errorType = stringField(record, "error_type") ?? stringField(record.detail, "error_type");
    const retryAfter = stringField(record, "retry_after") ?? stringField(record.detail, "retry_after");
    const provider = stringField(record, "provider") ?? stringField(record.detail, "provider");
    const path = stringField(record, "path") ?? stringField(record.detail, "path");
    if (errorType) meta.push(errorType);
    if (provider) meta.push(`provider ${provider}`);
    if (path) meta.push(path);
    if (retryAfter) meta.push(`retry ${retryAfter}s`);
  }
  return { message: error.message, meta };
}

function stringField(value: unknown, key: string): string | null {
  if (!value || typeof value !== "object") return null;
  const field = (value as Record<string, unknown>)[key];
  if (typeof field === "string" && field.trim()) return field;
  if (typeof field === "number" && Number.isFinite(field)) return String(field);
  return null;
}

function EmptyResearchState() {
  return (
    <Card>
      <CardContent className="flex min-h-[260px] flex-col items-center justify-center gap-3 text-center">
        <BrainCircuit className="h-8 w-8 text-primary" />
        <div>
          <p className="text-sm font-semibold uppercase">Ready for research</p>
          <p className="mt-1 max-w-xl text-sm text-muted-foreground">
            Run a query to generate a source-grounded market brief with cases, catalysts, risks, and watch items.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
