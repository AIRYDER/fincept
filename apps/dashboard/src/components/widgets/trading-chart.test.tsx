import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { TradingChart, type TradingChartPoint } from "@/components/widgets/trading-chart";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

const DATA: TradingChartPoint[] = [
  { x: 0, close: 100, high: 101, low: 99, volume: 1_000_000 },
  { x: 1, close: 102, high: 103, low: 101, volume: 1_200_000 },
  { x: 2, close: 101, high: 102, low: 100, volume: 900_000 },
  { x: 3, close: 105, high: 106, low: 104, volume: 1_500_000 },
];

test("renders the symbol label when provided", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} symbol="AAPL" />,
  );
  assert(html.includes("AAPL"));
});

test("renders the last close price", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} />,
  );
  assert(html.includes("$105.00"));
});

test("renders signed change and changePct for an advancing series", () => {
  // first=100, last=105 → change=+5, changePct=+5%
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} />,
  );
  assert(html.includes("+5.00"));
  assert(html.includes("+5.00%"));
});

test("renders signed change for a declining series", () => {
  const declining: TradingChartPoint[] = [
    { x: 0, close: 110 },
    { x: 1, close: 100 },
  ];
  const html = renderToStaticMarkup(
    <TradingChart data={declining} />,
  );
  // change = -10, changePct = -9.09%
  assert(html.includes("-10.00"));
  assert(html.includes("-9.09%"));
});

test("renders a MOCK chip when isMock is true", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} isMock />,
  );
  assert(html.includes("MOCK"));
});

test("does not render a MOCK chip when isMock is false", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} isMock={false} />,
  );
  assert(!html.includes("MOCK"));
});

test("renders range chips when onRangeChange is provided", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} range="1M" onRangeChange={() => undefined} />,
  );
  assert(html.includes("1D"));
  assert(html.includes("1W"));
  assert(html.includes("1M"));
  assert(html.includes("3M"));
  assert(html.includes("ALL"));
});

test("does not render range chips when onRangeChange is omitted", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} />,
  );
  assert(!html.includes(">1D<"));
});

test("empty data renders a placeholder, not a crash", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={[]} />,
  );
  assert(html.includes("No bars in this range."));
});

test("footer summary includes DATE and CLOSE labels when data is present", () => {
  const html = renderToStaticMarkup(
    <TradingChart data={DATA} />,
  );
  assert(html.includes("DATE"));
  assert(html.includes("CLOSE"));
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
  console.log(`${passed} trading-chart tests passed`);
}

void run();
