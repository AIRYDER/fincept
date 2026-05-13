/**
 * news-intelligence — pure read-only analyzer for the News Intelligence Command Center.
 *
 * Aggregates news feed, impact model status, positions, model promotion, and
 * services into a readiness summary with checks, stats, and symbol posture rows.
 * No mutations, no trading signals, no side effects.
 */

import type {
  NewsAlphaCandidateReportResponse,
  NewsArticle,
  NewsImpactStatus,
  NewsResponse,
  Position,
  PromotionStateResponse,
  ServicesResponse,
} from "@/lib/types";

export type NewsIntelState = "ready" | "review" | "blocked";
export type NewsIntelSeverity = "pass" | "watch" | "fail";

export interface NewsIntelCheck {
  id: string;
  label: string;
  severity: NewsIntelSeverity;
  detail: string;
}

export interface NewsIntelSymbolRow {
  symbol: string;
  inBook: boolean;
  alertCount: number;
  impactCount: number;
  latestAgeHours: number | null;
  hasImpactPrediction: boolean;
}

export interface NewsIntelSummary {
  state: NewsIntelState;
  score: number;
  headline: string;
  checks: NewsIntelCheck[];
  actions: string[];
  stats: {
    alertCount: number;
    impactCount: number;
    universeCount: number;
    bookSymbols: number;
    impactModelLoaded: boolean;
    impactModelMode: string | null;
    impactEventCount: number | null;
    newsAlphaPromoted: boolean;
    newsAlphaCandidateApproved: boolean | null;
    labelCoverage: string | null;
  };
  symbols: NewsIntelSymbolRow[];
}

// ---------------------------------------------------------------------------
// Main builder
// ---------------------------------------------------------------------------

export function buildNewsIntelligence({
  news,
  impactStatus,
  positions,
  promotion,
  newsAlphaReport,
  services,
}: {
  news: NewsResponse | null;
  impactStatus: NewsImpactStatus | null;
  positions: Position[];
  promotion?: PromotionStateResponse | null;
  newsAlphaReport?: NewsAlphaCandidateReportResponse | null;
  services?: ServicesResponse | null;
}): NewsIntelSummary {
  const stats = buildStats(news, impactStatus, positions, promotion, newsAlphaReport);
  const symbols = buildSymbols(news, positions);
  const checks = buildChecks({ news, impactStatus, positions, promotion, newsAlphaReport, services, stats });
  const failed = checks.filter((c) => c.severity === "fail").length;
  const watches = checks.filter((c) => c.severity === "watch").length;
  const state: NewsIntelState = failed > 0 ? "blocked" : watches > 0 ? "review" : "ready";
  const score = Math.max(0, 100 - failed * 30 - watches * 10);

  const headline =
    state === "ready"
      ? "News intelligence operational"
      : state === "review"
        ? "News intelligence needs attention"
        : "News intelligence blocked";

  const actions: string[] = [];
  if (!impactStatus?.dataset_loaded) actions.push("Load historical outcomes for impact model");
  if (stats.newsAlphaCandidateApproved === false) actions.push("Review news-alpha candidate report before promotion");
  if (stats.alertCount > 0) actions.push("Inspect alert-lane stories for adverse book impact");
  if (!news) actions.push("Verify news feed connectivity");

  return { state, score, headline, checks, actions, stats, symbols };
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

function buildStats(
  news: NewsResponse | null,
  impactStatus: NewsImpactStatus | null,
  positions: Position[],
  promotion?: PromotionStateResponse | null,
  newsAlphaReport?: NewsAlphaCandidateReportResponse | null,
): NewsIntelSummary["stats"] {
  const bookSymbols = new Set(
    positions.filter((p) => Number(p.quantity) !== 0).map((p) => p.symbol),
  );

  const newsAlphaPromoted =
    promotion?.active?.model_name?.toLowerCase().includes("news") ?? false;
  const newsAlphaCandidateApproved = newsAlphaReport?.report?.approved ?? null;

  // Label coverage: infer from impact model profile if available
  let labelCoverage: string | null = null;
  if (impactStatus?.profile) {
    const count = impactStatus.profile.event_count;
    labelCoverage = count > 0 ? `${count} labeled outcomes` : "No labeled outcomes";
  }

  return {
    alertCount: news?.alert?.length ?? 0,
    impactCount: news?.impact?.length ?? 0,
    universeCount: news?.universe?.length ?? 0,
    bookSymbols: bookSymbols.size,
    impactModelLoaded: impactStatus?.dataset_loaded ?? false,
    impactModelMode: impactStatus?.mode ?? null,
    impactEventCount: impactStatus?.profile?.event_count ?? null,
    newsAlphaPromoted,
    newsAlphaCandidateApproved,
    labelCoverage,
  };
}

// ---------------------------------------------------------------------------
// Symbol posture rows
// ---------------------------------------------------------------------------

function buildSymbols(
  news: NewsResponse | null,
  positions: Position[],
): NewsIntelSymbolRow[] {
  const bookSymbols = new Set(
    positions.filter((p) => Number(p.quantity) !== 0).map((p) => p.symbol),
  );
  if (!news) return [];

  const allArticles: NewsArticle[] = [
    ...(news.alert ?? []),
    ...(news.impact ?? []),
    ...(news.universe ?? []),
  ];

  const map = new Map<string, NewsIntelSymbolRow>();
  for (const article of allArticles) {
    for (const s of article.symbols) {
      const existing = map.get(s.symbol);
      const isAlert = news.alert?.some((a) => a.id === article.id) ?? false;
      const isImpact = news.impact?.some((a) => a.id === article.id) ?? false;
      if (existing) {
        if (isAlert) existing.alertCount += 1;
        if (isImpact) existing.impactCount += 1;
        const ageH = article.age_hours;
        if (ageH != null) {
          if (existing.latestAgeHours === null || ageH < existing.latestAgeHours) {
            existing.latestAgeHours = ageH;
          }
        }
      } else {
        map.set(s.symbol, {
          symbol: s.symbol,
          inBook: bookSymbols.has(s.symbol),
          alertCount: isAlert ? 1 : 0,
          impactCount: isImpact ? 1 : 0,
          latestAgeHours: article.age_hours ?? null,
          hasImpactPrediction: false,
        });
      }
    }
  }

  // Sort: book symbols first, then by alertCount desc, then by symbol
  return Array.from(map.values()).sort((a, b) => {
    if (a.inBook !== b.inBook) return a.inBook ? -1 : 1;
    if (a.alertCount !== b.alertCount) return b.alertCount - a.alertCount;
    return a.symbol.localeCompare(b.symbol);
  });
}

// ---------------------------------------------------------------------------
// Checks
// ---------------------------------------------------------------------------

function buildChecks({
  news,
  impactStatus,
  positions,
  promotion,
  newsAlphaReport,
  services,
  stats,
}: {
  news: NewsResponse | null;
  impactStatus: NewsImpactStatus | null;
  positions: Position[];
  promotion?: PromotionStateResponse | null;
  newsAlphaReport?: NewsAlphaCandidateReportResponse | null;
  services?: ServicesResponse | null;
  stats: NewsIntelSummary["stats"];
}): NewsIntelCheck[] {
  const checks: NewsIntelCheck[] = [];

  // 1. News feed presence
  if (!news) {
    checks.push({ id: "news-feed", label: "News feed", severity: "fail", detail: "No news data available" });
  } else {
    const total = stats.alertCount + stats.impactCount + stats.universeCount;
    if (total === 0) {
      checks.push({ id: "news-feed", label: "News feed", severity: "watch", detail: "Feed returned zero articles" });
    } else {
      checks.push({ id: "news-feed", label: "News feed", severity: "pass", detail: `${total} articles across 3 lanes` });
    }
  }

  // 2. Impact model availability
  if (!impactStatus) {
    checks.push({ id: "impact-model", label: "Impact model", severity: "watch", detail: "Impact model status unavailable" });
  } else if (!impactStatus.dataset_loaded) {
    checks.push({ id: "impact-model", label: "Impact model", severity: "watch", detail: "Historical outcomes not loaded" });
  } else {
    const count = impactStatus.profile?.event_count ?? 0;
    checks.push({
      id: "impact-model",
      label: "Impact model",
      severity: "pass",
      detail: `Loaded with ${count} labeled outcomes (${impactStatus.mode})`,
    });
  }

  // 3. Label coverage
  if (stats.labelCoverage === null) {
    checks.push({ id: "label-coverage", label: "Label coverage", severity: "watch", detail: "Label state unknown — impact model status missing" });
  } else if (stats.impactEventCount !== null && stats.impactEventCount < 10) {
    checks.push({ id: "label-coverage", label: "Label coverage", severity: "watch", detail: `${stats.impactEventCount} labeled outcomes — sparse for reliable analogs` });
  } else {
    checks.push({ id: "label-coverage", label: "Label coverage", severity: "pass", detail: stats.labelCoverage });
  }

  // 4. News-alpha promotion gate
  if (stats.newsAlphaPromoted) {
    checks.push({ id: "news-alpha", label: "News-alpha model", severity: "pass", detail: "News-alpha model is actively promoted" });
  } else if (stats.newsAlphaCandidateApproved === false) {
    checks.push({ id: "news-alpha", label: "News-alpha model", severity: "watch", detail: "Candidate report exists but not approved for promotion" });
  } else if (stats.newsAlphaCandidateApproved === null) {
    checks.push({ id: "news-alpha", label: "News-alpha model", severity: "watch", detail: "No candidate report — news-alpha not promotable" });
  }

  // 5. Book exposure to alerts
  if (stats.alertCount > 0) {
    checks.push({
      id: "book-alerts",
      label: "Book alerts",
      severity: "watch",
      detail: `${stats.alertCount} alert-tier stories touching your book`,
    });
  } else {
    checks.push({ id: "book-alerts", label: "Book alerts", severity: "pass", detail: "No alert-tier stories" });
  }

  // 6. Predictor service (if services provided)
  if (services) {
    const predictor = services.services.find(
      (s) => s.name === "gbm_predictor" || s.name === "news_alpha_predictor",
    );
    if (predictor && predictor.status !== "up") {
      checks.push({ id: "predictor-service", label: "Predictor service", severity: "watch", detail: `${predictor.name} is ${predictor.status}` });
    } else if (predictor) {
      checks.push({ id: "predictor-service", label: "Predictor service", severity: "pass", detail: `${predictor.name} is up` });
    }
  }

  return checks;
}
