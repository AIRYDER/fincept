import assert from "assert";

import type {
  NewsAlphaCandidateReportResponse,
  NewsImpactStatus,
  NewsResponse,
  Position,
  PromotionStateResponse,
  ServicesResponse,
} from "@/lib/types";

import { buildNewsIntelligence } from "./news-intelligence";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const news: NewsResponse = {
  alert: [],
  impact: [
    {
      id: "a1",
      headline: "NVDA earnings beat",
      summary: "",
      source: "benzinga",
      url: "",
      author: "",
      created_at: "",
      ts_event_ns: 1_000_000_000,
      symbols: [
        { symbol: "NVDA", in_book: true, price_at_publish: "100", mark: "105", pct_change: 0.05, dollar_impact: "500", sparkline: [] },
      ],
      touched_book: true,
      has_impact_math: true,
      total_dollar_impact: "500",
      is_adverse: false,
      age_hours: 1.0,
      score: 500,
      pct_of_book: "0.01",
      tier: "impact",
    },
  ],
  universe: [
    {
      id: "a2",
      headline: "Market update",
      summary: "",
      source: "reuters",
      url: "",
      author: "",
      created_at: "",
      ts_event_ns: 900_000_000,
      symbols: [
        { symbol: "SPY", in_book: false, price_at_publish: null, mark: null, pct_change: null, dollar_impact: null, sparkline: [] },
      ],
      touched_book: false,
      has_impact_math: false,
      total_dollar_impact: null,
      is_adverse: false,
      age_hours: 2.0,
      score: 0,
      pct_of_book: null,
      tier: "universe",
    },
  ],
  book_symbols: ["NVDA"],
  book_total_impact: "500",
  book_equity_usd: "50000",
  alert_pct_of_book: 0.005,
  recency_half_life_h: 12,
};

const impactStatus: NewsImpactStatus = {
  app: "news-impact-model",
  dataset_loaded: true,
  profile: {
    path: "/sample_data/historical_outcomes.jsonl",
    event_count: 42,
    horizons: ["5m", "30m", "1h"],
    sources: { benzinga: 20, reuters: 22 },
    event_types: { earnings: 15, guidance: 27 },
    symbols: { NVDA: 10, AAPL: 32 },
  },
  last_optimization: null,
  experiment_root: "/experiments/news-impact-model",
  sample_data: "/sample_data",
  mode: "experimental_demo",
};

const positions: Position[] = [
  {
    strategy_id: "s1",
    symbol: "NVDA",
    quantity: "100",
    avg_cost: "90",
    realized_pnl: "0",
    unrealized_pnl: "1500",
    updated_at: 1,
  },
];

const promotion: PromotionStateResponse = {
  agent_id: "news_alpha_predictor",
  active: null,
  shadow: null,
  history: [],
};

const newsAlphaReport: NewsAlphaCandidateReportResponse = {
  exists: true,
  report_path: "/models/news-alpha/candidate-report",
  report: {
    approved: false,
    reasons: ["AUC below threshold"],
    candidate_model_name: "news_alpha_v2",
    candidate_dir: "/models/news_alpha_v2",
    candidate_meta: {},
    active_model_name: null,
    active_meta: null,
    policy: {},
    generated_at: 1,
    promotion_hint: {},
  },
};

const services: ServicesResponse = {
  services: [
    { name: "gbm_predictor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
    { name: "news_alpha_predictor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
  ],
  summary: { up: 2, expected: 2, stale_after_sec: 30, ttl_sec: 90 },
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("builds a ready summary with healthy news and impact model", () => {
  const approvedReport: NewsAlphaCandidateReportResponse = {
    exists: true,
    report_path: "/models/news-alpha/candidate-report",
    report: {
      ...newsAlphaReport.report!,
      approved: true,
      reasons: [],
    },
  };
  const summary = buildNewsIntelligence({
    news,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport: approvedReport,
    services,
  });
  assert.equal(summary.state, "ready");
  assert.equal(summary.stats.alertCount, 0);
  assert.equal(summary.stats.impactCount, 1);
  assert.equal(summary.stats.universeCount, 1);
  assert.equal(summary.stats.impactModelLoaded, true);
  assert.equal(summary.stats.impactEventCount, 42);
  assert.equal(summary.stats.bookSymbols, 1);
  assert.equal(summary.symbols.length, 2); // NVDA + SPY
  assert(summary.checks.some((c) => c.id === "news-feed" && c.severity === "pass"));
  assert(summary.checks.some((c) => c.id === "impact-model" && c.severity === "pass"));
  assert(summary.checks.some((c) => c.id === "label-coverage" && c.severity === "pass"));
});

test("blocks when news feed is unavailable", () => {
  const summary = buildNewsIntelligence({
    news: null,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport,
    services,
  });
  assert.equal(summary.state, "blocked");
  assert(summary.checks.some((c) => c.id === "news-feed" && c.severity === "fail"));
});

test("marks review when impact model is not loaded", () => {
  const summary = buildNewsIntelligence({
    news,
    impactStatus: { ...impactStatus, dataset_loaded: false },
    positions,
    promotion,
    newsAlphaReport,
    services,
  });
  assert.equal(summary.state, "review");
  assert(summary.checks.some((c) => c.id === "impact-model" && c.severity === "watch"));
});

test("marks review when alert-tier stories exist", () => {
  const alertNews: NewsResponse = {
    ...news,
    alert: [
      {
        id: "a0",
        headline: "NVDA downgrade",
        summary: "",
        source: "benzinga",
        url: "",
        author: "",
        created_at: "",
        ts_event_ns: 1_100_000_000,
        symbols: [
          { symbol: "NVDA", in_book: true, price_at_publish: "100", mark: "90", pct_change: -0.1, dollar_impact: "-1000", sparkline: [] },
        ],
        touched_book: true,
        has_impact_math: true,
        total_dollar_impact: "-1000",
        is_adverse: true,
        age_hours: 0.5,
        score: 1300,
        pct_of_book: "0.02",
        tier: "alert",
      },
    ],
  };
  const summary = buildNewsIntelligence({
    news: alertNews,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport,
    services,
  });
  assert.equal(summary.stats.alertCount, 1);
  assert(summary.checks.some((c) => c.id === "book-alerts" && c.severity === "watch"));
  assert(summary.symbols.some((s) => s.symbol === "NVDA" && s.alertCount > 0));
});

test("marks review when news-alpha candidate is not approved", () => {
  const summary = buildNewsIntelligence({
    news,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport,
    services,
  });
  assert.equal(summary.stats.newsAlphaCandidateApproved, false);
  assert(summary.checks.some((c) => c.id === "news-alpha" && c.severity === "watch"));
});

test("shows news-alpha promoted when active model includes news", () => {
  const promoted: PromotionStateResponse = {
    agent_id: "news_alpha_predictor",
    active: {
      model_name: "news_alpha_v2",
      promoted_by: "operator",
      promoted_at: 1,
      agent_id: "news_alpha_predictor",
    },
    shadow: null,
    history: [],
  };
  const summary = buildNewsIntelligence({
    news,
    impactStatus,
    positions,
    promotion: promoted,
    newsAlphaReport,
    services,
  });
  assert.equal(summary.stats.newsAlphaPromoted, true);
  assert(summary.checks.some((c) => c.id === "news-alpha" && c.severity === "pass"));
});

test("marks review when predictor service is down", () => {
  const downServices: ServicesResponse = {
    services: [
      { name: "gbm_predictor", status: "down", last_beat_unix: null, age_sec: null, expected: true },
    ],
    summary: { up: 0, expected: 1, stale_after_sec: 30, ttl_sec: 90 },
  };
  const summary = buildNewsIntelligence({
    news,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport,
    services: downServices,
  });
  assert(summary.checks.some((c) => c.id === "predictor-service" && c.severity === "watch"));
});

test("sorts symbol posture with book symbols first", () => {
  const summary = buildNewsIntelligence({
    news,
    impactStatus,
    positions,
    promotion,
    newsAlphaReport,
    services,
  });
  assert.equal(summary.symbols[0].symbol, "NVDA");
  assert.equal(summary.symbols[0].inBook, true);
  assert.equal(summary.symbols[1].symbol, "SPY");
  assert.equal(summary.symbols[1].inBook, false);
});

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

async function run() {
  let passed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      passed += 1;
      console.log(`ok - ${name}`);
    } catch (error) {
      console.error(`not ok - ${name}`);
      console.error(error);
    }
  }
  console.log(`${passed} news intelligence tests passed`);
}

run();
