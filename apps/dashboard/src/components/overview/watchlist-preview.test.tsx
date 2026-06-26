import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { WatchlistPreview } from "@/components/overview/watchlist-preview";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

test("renders the Watchlist title and a MOCK badge", () => {
  const html = renderToStaticMarkup(<WatchlistPreview />);
  assert(html.includes("Watchlist"));
  assert(html.includes("Mock"));
});

test("renders all six base symbols", () => {
  const html = renderToStaticMarkup(<WatchlistPreview />);
  for (const sym of ["AAPL", "NVDA", "META", "TSLA", "AMD", "COIN"]) {
    assert(html.includes(sym), `expected ${sym} in preview`);
  }
});

test("renders the advance/decline summary counts", () => {
  const html = renderToStaticMarkup(<WatchlistPreview />);
  // The summary band renders "N up · N down · N% adv".
  assert(html.includes("up"));
  assert(html.includes("down"));
  assert(html.includes("adv"));
});

test("renders the column header row", () => {
  const html = renderToStaticMarkup(<WatchlistPreview />);
  assert(html.includes("Symbol"));
  assert(html.includes("Last"));
  assert(html.includes("Trend"));
});

test("renders a link to /watchlist", () => {
  const html = renderToStaticMarkup(<WatchlistPreview />);
  assert(html.includes('href="/watchlist"'));
});

test("renders per-row links to /symbol/{symbol}", () => {
  const html = renderToStaticMarkup(<WatchlistPreview />);
  assert(html.includes('href="/symbol/AAPL"'));
  assert(html.includes('href="/symbol/NVDA"'));
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
  console.log(`${passed} watchlist-preview tests passed`);
}

void run();
