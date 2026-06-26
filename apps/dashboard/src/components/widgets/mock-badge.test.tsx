import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { MockBadge } from "@/components/widgets/mock-badge";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

test("renders the Mock label with a dashed amber border", () => {
  const html = renderToStaticMarkup(<MockBadge />);
  // Default size renders "Mock" (the corner size renders "MOCK").
  assert(html.includes("Mock"));
  assert(html.includes("border-dashed"));
  assert(html.includes("border-warn"));
  assert(html.includes("text-warn"));
});

test("renders the source label when provided", () => {
  const html = renderToStaticMarkup(<MockBadge source="Inline fixture" />);
  assert(html.includes("Mock"));
  assert(html.includes("Inline fixture"));
});

test("omits the source label when not provided", () => {
  const html = renderToStaticMarkup(<MockBadge />);
  // No " · " separator should appear without a source.
  assert(!html.includes("·"));
});

test("includes the ticket in the title tooltip when provided", () => {
  const html = renderToStaticMarkup(<MockBadge source="Seed" ticket="FIN-1234" />);
  assert(html.includes("FIN-1234"));
  assert(html.includes('title="'));
});

test("corner size renders only MOCK as visible text (source stays in tooltip)", () => {
  const html = renderToStaticMarkup(<MockBadge size="corner" source="Ignored" />);
  assert(html.includes("MOCK"));
  // The corner variant does not render the "Mock · source" visible
  // suffix; the source only appears inside the title tooltip attr.
  assert(!html.includes(">Mock"));
  assert(!html.includes("· Ignored"));
});

test("inline size uses smaller padding than default", () => {
  const inline = renderToStaticMarkup(<MockBadge size="inline" />);
  const def = renderToStaticMarkup(<MockBadge size="default" />);
  // Both render MOCK; the inline variant uses text-[9px] vs text-[10px].
  assert(inline.includes("text-[9px]"));
  assert(def.includes("text-[10px]"));
});

test("every variant carries the flask icon", () => {
  for (const size of ["default", "inline", "corner"] as const) {
    const html = renderToStaticMarkup(<MockBadge size={size} />);
    // The flask icon is an inline SVG; renderToStaticMarkup emits it as
    // an <svg> element.  We just assert the svg tag is present.
    assert(html.includes("<svg"), `expected svg icon for size=${size}`);
  }
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
  console.log(`${passed} mock-badge tests passed`);
}

void run();
