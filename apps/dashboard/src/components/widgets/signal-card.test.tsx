import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { SignalCard, SignalStrip, type SignalKind } from "@/components/widgets/signal-card";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

test("renders the title, symbol, and kind label", () => {
  const html = renderToStaticMarkup(
    <SignalCard
      kind="prediction"
      title="Momentum turned bullish"
      symbol="AAPL"
      direction={0.62}
      confidence={0.71}
    />,
  );
  assert(html.includes("AAPL"));
  assert(html.includes("Momentum turned bullish"));
  assert(html.includes("PREDICTION"));
});

test("renders the AI source badge for model signals", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" source="model" />,
  );
  assert(html.includes("AI"));
});

test("renders the SYSTEM source badge for system signals", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="signal" title="t" source="system" />,
  );
  assert(html.includes("SYSTEM"));
});

test("renders the HUMAN source badge for human signals", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="signal" title="t" source="human" />,
  );
  assert(html.includes("HUMAN"));
});

test("renders a MOCK chip when isMock is true", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" isMock />,
  );
  assert(html.includes("MOCK"));
});

test("does not render a MOCK chip when isMock is false", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" isMock={false} />,
  );
  assert(!html.includes("MOCK"));
});

test("alert kind renders severity badge instead of source badge", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="alert" title="Risk breach" severity="critical" />,
  );
  assert(html.includes("ALERT"));
  assert(html.includes("CRIT"));
});

test("every kind maps to a distinct label", () => {
  const kinds: SignalKind[] = ["prediction", "alert", "signal"];
  const labels = kinds.map((k) =>
    renderToStaticMarkup(<SignalCard kind={k} title="t" />),
  );
  assert(labels[0].includes("PREDICTION"));
  assert(labels[1].includes("ALERT"));
  assert(labels[2].includes("SIGNAL"));
});

test("direction bar is omitted when direction is null", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" direction={null} />,
  );
  // The direction bar container has a rounded-full track; when
  // direction is null the whole block is skipped.
  assert(!html.includes("bg-muted/40"));
});

test("direction bar is rendered when direction is provided", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" direction={0.5} confidence={0.8} />,
  );
  assert(html.includes("bg-muted/40"));
});

test("positive direction renders the long trend icon and + sign", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" direction={0.5} confidence={0.8} />,
  );
  assert(html.includes("text-long"));
  assert(html.includes("+0.50"));
});

test("negative direction renders the short trend icon and no + sign", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" direction={-0.5} confidence={0.8} />,
  );
  assert(html.includes("text-short"));
  assert(html.includes("-0.50"));
  assert(!html.includes("+−") && !html.includes("+-0.50"));
});

test("confidence is rendered as a percentage", () => {
  const html = renderToStaticMarkup(
    <SignalCard kind="prediction" title="t" direction={0.5} confidence={0.71} />,
  );
  assert(html.includes("conf 71%"));
});

test("metric line is rendered when metric is provided", () => {
  const html = renderToStaticMarkup(
    <SignalCard
      kind="prediction"
      title="t"
      metric={{ label: "Target", value: "$200", tone: "long" }}
    />,
  );
  assert(html.includes("Target"));
  assert(html.includes("$200"));
});

test("SignalStrip renders symbol, direction, and confidence", () => {
  const html = renderToStaticMarkup(
    <SignalStrip direction={0.62} confidence={0.71} symbol="AAPL" />,
  );
  assert(html.includes("AAPL"));
  assert(html.includes("+0.62"));
  assert(html.includes("71%"));
});

test("SignalStrip: negative direction renders - sign", () => {
  const html = renderToStaticMarkup(
    <SignalStrip direction={-0.3} confidence={0.5} symbol="TSLA" />,
  );
  assert(html.includes("TSLA"));
  // formatNumber uses toLocaleString with minimumFractionDigits: 0,
  // so -0.3 renders as "-0.3" (not "-0.30").
  assert(html.includes("-0.3"));
  assert(!html.includes("+"));
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
  console.log(`${passed} signal-card tests passed`);
}

void run();
