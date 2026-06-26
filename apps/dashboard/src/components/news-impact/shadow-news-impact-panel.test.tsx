import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { NewsImpactSignalsResponse } from "@/lib/types";

import { ShadowNewsImpactPanel } from "./shadow-news-impact-panel";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const response: NewsImpactSignalsResponse = {
  stream: "sig.news_impact",
  count: 1,
  signals: [
    {
      stream_id: "1747500000000-0",
      type: "news_impact",
      published_at: "1747500000000000000",
      payload: {
        schema_version: 1,
        agent_id: "news_impact_agent.v1",
        event_id: "evt-guidance-1",
        symbol: "ACME",
        ts_event: 1_747_500_000_000_000_000,
        available_at_ns: 1_747_500_000_000_000_000,
        event_type: "guidance",
        confidence: 0.72,
        horizons: {
          "5m": {
            expected_return: 0.012,
            p_up: 0.68,
            q10: -0.004,
            q50: 0.01,
            q90: 0.027,
            sample_size: 14,
          },
          "30m": {
            expected_return: -0.003,
            p_up: 0.44,
            q10: -0.018,
            q50: -0.002,
            q90: 0.011,
            sample_size: 11,
          },
        },
        source_urls: ["https://example.com/acme-guidance"],
        similar_event_ids: ["hist-earnings-1", "hist-guidance-2"],
        model_version: "news-impact-analog-baseline-v0",
        metadata: {
          source: "benzinga",
          headline: "ACME raises full-year guidance",
        },
      },
    },
  ],
};

test("renders event, evidence, horizons, and the shadow-only badge", () => {
  const html = renderToStaticMarkup(
    <ShadowNewsImpactPanel response={response} isLoading={false} />,
  );

  assert(html.includes("Shadow only / not trade-driving"));
  assert(html.includes("ACME raises full-year guidance"));
  assert(html.includes("ACME"));
  assert(html.includes("72%"));
  assert(html.includes("5m"));
  assert(html.includes("+1.20%"));
  assert(html.includes("68%"));
  assert(html.includes("https://example.com/acme-guidance"));
  assert(html.includes("hist-earnings-1"));
  assert(html.includes("news-impact-analog-baseline-v0"));
});

test("does not render trade-driving controls or sizing fields", () => {
  const html = renderToStaticMarkup(
    <ShadowNewsImpactPanel response={response} isLoading={false} />,
  ).toLowerCase();

  const forbidden = [
    "buy",
    "sell",
    "place order",
    "submit order",
    "quantity",
    "target_notional_usd",
    "position size",
    "sizing",
    "broker",
  ];

  for (const term of forbidden) {
    assert(!html.includes(term), `rendered forbidden trade term: ${term}`);
  }
});

test("renders an empty shadow stream state", () => {
  const html = renderToStaticMarkup(
    <ShadowNewsImpactPanel
      response={{ stream: "sig.news_impact", count: 0, signals: [] }}
      isLoading={false}
    />,
  );

  assert(html.includes("No shadow news-impact signals yet"));
  assert(html.includes("sig.news_impact"));
});

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
      process.exitCode = 1;
      return;
    }
  }
  console.log(`${passed} shadow news-impact panel tests passed`);
}

void run();
