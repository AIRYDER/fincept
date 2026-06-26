import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { WatchlistRow, WATCHLIST_BRAND } from "@/components/widgets/watchlist-row";
import { BRAND } from "@/lib/design-tokens";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const SPARKLINE = [
  { x: 0, y: 100 },
  { x: 1, y: 101 },
  { x: 2, y: 102 },
  { x: 3, y: 103 },
];

test("renders symbol, name, and last price", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      name="Apple Inc."
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
    />,
  );
  assert(html.includes("AAPL"));
  assert(html.includes("Apple Inc."));
  assert(html.includes("$192.34"));
});

test("renders signed USD change with + sign for advancing rows", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
    />,
  );
  assert(html.includes("+$1.23"), "expected signed +$1.23 for positive change");
  assert(html.includes("+0.64%"), "expected signed +0.64% for positive changePct");
});

test("renders signed USD change with - sign for declining rows", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="TSLA"
      last={248.2}
      change={-2.5}
      changePct={-1.0}
      sparkline={SPARKLINE}
    />,
  );
  assert(html.includes("-$2.50"), "expected signed -$2.50 for negative change");
  assert(html.includes("-1.00%"), "expected signed -1.00% for negative changePct");
});

test("renders em dash for non-finite change values (never throws)", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={Number.NaN}
      changePct={Number.NaN}
      sparkline={SPARKLINE}
    />,
  );
  assert(html.includes("—"), "expected em dash for non-finite change");
});

test("renders a MOCK chip when isMock is true", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
      isMock
    />,
  );
  assert(html.includes("MOCK"));
});

test("does not render a MOCK chip when isMock is false", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
      isMock={false}
    />,
  );
  assert(!html.includes("MOCK"));
});

test("renders a cap badge when cap is provided", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
      cap="MEGA"
    />,
  );
  assert(html.includes("MEGA"));
});

test("links to /symbol/{symbol} by default", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
    />,
  );
  assert(html.includes('href="/symbol/AAPL"'));
});

test("encodes the symbol in the default href", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="A/B"
      last={1}
      change={0}
      changePct={0}
      sparkline={SPARKLINE}
    />,
  );
  assert(html.includes('href="/symbol/A%2FB"'));
});

test("respects a custom href override", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={SPARKLINE}
      href="/custom/path"
    />,
  );
  assert(html.includes('href="/custom/path"'));
});

test("renders a placeholder when sparkline has fewer than 2 points", () => {
  const html = renderToStaticMarkup(
    <WatchlistRow
      symbol="AAPL"
      last={192.34}
      change={1.23}
      changePct={0.64}
      sparkline={[{ x: 0, y: 100 }]}
    />,
  );
  // The sparkline slot renders an em dash placeholder when there is
  // not enough data to draw a line.
  assert(html.includes(">—<") || html.includes(">—"));
});

test("WATCHLIST_BRAND re-exports the canonical BRAND identity", () => {
  assert.equal(WATCHLIST_BRAND, BRAND);
  assert.equal(WATCHLIST_BRAND.name, BRAND.name);
  assert.equal(WATCHLIST_BRAND.accent, "cobalt");
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
  console.log(`${passed} watchlist-row tests passed`);
}

void run();
