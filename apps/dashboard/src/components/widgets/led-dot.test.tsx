import assert from "assert";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { DotMatrix, LEDDot, type LEDTone } from "@/components/widgets/led-dot";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

test("LEDDot renders a rounded dot with a tone class", () => {
  const html = renderToStaticMarkup(<LEDDot tone="long" />);
  assert(html.includes("rounded-full"));
  assert(html.includes("bg-long"));
});

test("LEDDot: every tone maps to a bg + text class", () => {
  const tones: LEDTone[] = ["long", "short", "warn", "info", "cyan", "muted"];
  for (const tone of tones) {
    const html = renderToStaticMarkup(<LEDDot tone={tone} />);
    assert(html.includes(`bg-${tone}`), `expected bg-${tone} for tone=${tone}`);
  }
});

test("LEDDot: pulse adds the animate-pulse class", () => {
  const html = renderToStaticMarkup(<LEDDot pulse />);
  assert(html.includes("animate-pulse"));
});

test("LEDDot: without pulse does not animate", () => {
  const html = renderToStaticMarkup(<LEDDot pulse={false} />);
  assert(!html.includes("animate-pulse"));
});

test("LEDDot: sm / md / lg size classes differ", () => {
  const sm = renderToStaticMarkup(<LEDDot size="sm" />);
  const md = renderToStaticMarkup(<LEDDot size="md" />);
  const lg = renderToStaticMarkup(<LEDDot size="lg" />);
  assert(sm !== md);
  assert(md !== lg);
  assert(sm !== lg);
});

test("LEDDot: title becomes aria-label for accessibility", () => {
  const html = renderToStaticMarkup(<LEDDot title="Live status" />);
  assert(html.includes('aria-label="Live status"'));
});

test("DotMatrix renders its children", () => {
  const html = renderToStaticMarkup(
    <DotMatrix>AAPL 192.34</DotMatrix>,
  );
  assert(html.includes("AAPL 192.34"));
  assert(html.includes("dot-matrix"));
});

test("DotMatrix: fade=false omits the mask image style", () => {
  const html = renderToStaticMarkup(
    <DotMatrix fade={false}>no fade</DotMatrix>,
  );
  assert(!html.includes("maskImage"));
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
  console.log(`${passed} led-dot tests passed`);
}

void run();
