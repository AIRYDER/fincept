import assert from "assert";

import {
  buildEntityResults,
  COMMANDS,
  filterCommands,
  filterEntities,
  type PaletteCommand,
} from "./command-registry";

type TestFn = () => void | Promise<void>;

const tests: Array<{ name: string; fn: TestFn }> = [];

function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("COMMANDS has entries for navigate, search, action, and dangerous categories", () => {
  const categories = new Set(COMMANDS.map((c) => c.category));
  assert(categories.has("navigate"));
  assert(categories.has("search"));
  assert(categories.has("action"));
  assert(categories.has("dangerous"));
});

test("every command has required fields", () => {
  for (const cmd of COMMANDS) {
    assert(cmd.id, `missing id`);
    assert(cmd.label, `missing label on ${cmd.id}`);
    assert(cmd.category, `missing category on ${cmd.id}`);
    assert(cmd.safety, `missing safety on ${cmd.id}`);
    assert(cmd.icon, `missing icon on ${cmd.id}`);
    assert(cmd.href, `missing href on ${cmd.id}`);
    assert(cmd.keywords.length > 0, `missing keywords on ${cmd.id}`);
    assert(cmd.group, `missing group on ${cmd.id}`);
  }
});

test("dangerous commands all have confirm safety", () => {
  const dangerous = COMMANDS.filter((c) => c.category === "dangerous");
  assert(dangerous.length > 0, "no dangerous commands");
  for (const cmd of dangerous) {
    assert.equal(cmd.safety, "confirm", `${cmd.id} should be confirm safety`);
  }
});

test("dangerous commands never execute directly — href includes query param", () => {
  const dangerous = COMMANDS.filter((c) => c.category === "dangerous");
  for (const cmd of dangerous) {
    // Dangerous commands route to confirmation pages with ?action= params
    assert(cmd.href.includes("?action="), `${cmd.id} should have ?action= param`);
  }
});

test("no command directly places orders or kills switch", () => {
  // No href should be a direct API call
  for (const cmd of COMMANDS) {
    assert(!cmd.href.startsWith("/api/"), `${cmd.id} should not call API directly`);
    assert(!cmd.href.includes("kill_now"), `${cmd.id} should not have kill_now`);
    assert(!cmd.href.includes("place_order"), `${cmd.id} should not have place_order`);
  }
});

test("filterCommands matches by label", () => {
  const results = filterCommands("positions");
  assert(results.some((c) => c.id === "nav:positions"));
});

test("filterCommands matches by mnemonic", () => {
  const results = filterCommands("OV");
  assert(results.some((c) => c.id === "nav:overview"));
});

test("filterCommands matches by keyword", () => {
  const results = filterCommands("audit");
  assert(results.some((c) => c.id === "action:recon-checklist"));
});

test("filterCommands returns all when query is empty", () => {
  const results = filterCommands("");
  assert.equal(results.length, COMMANDS.length);
});

test("buildEntityResults creates symbol entities", () => {
  const entities = buildEntityResults(["AAPL", "NVDA"], [], []);
  assert.equal(entities.length, 2);
  assert(entities.every((e) => e.type === "symbol"));
  assert(entities[0].href.includes("AAPL"));
});

test("buildEntityResults creates strategy entities", () => {
  const entities = buildEntityResults([], ["strat_a"], []);
  assert.equal(entities.length, 1);
  assert.equal(entities[0].type, "strategy");
  assert(entities[0].href.includes("strat_a"));
});

test("buildEntityResults creates model entities", () => {
  const entities = buildEntityResults([], [], ["gbm_v1"]);
  assert.equal(entities.length, 1);
  assert.equal(entities[0].type, "model");
  assert(entities[0].href.includes("gbm_v1"));
});

test("filterEntities returns empty for empty query", () => {
  const entities = buildEntityResults(["AAPL"], [], []);
  const filtered = filterEntities(entities, "");
  assert.equal(filtered.length, 0);
});

test("filterEntities matches by label", () => {
  const entities = buildEntityResults(["AAPL", "NVDA"], [], []);
  const filtered = filterEntities(entities, "AAPL");
  assert.equal(filtered.length, 1);
  assert.equal(filtered[0].label, "AAPL");
});

test("filterEntities matches by keyword", () => {
  const entities = buildEntityResults(["AAPL"], [], []);
  const filtered = filterEntities(entities, "ticker");
  assert.equal(filtered.length, 1);
});

test("navigate commands cover all major pages", () => {
  const navIds = COMMANDS.filter((c) => c.category === "navigate").map((c) => c.id);
  const expectedPages = ["overview", "positions", "orders", "recon", "strategies", "news", "backtest", "models", "receipts", "risk"];
  for (const page of expectedPages) {
    assert(navIds.some((id) => id.includes(page)), `missing nav for ${page}`);
  }
});

test("action commands include read-only checks and refreshes", () => {
  const actionIds = COMMANDS.filter((c) => c.category === "action").map((c) => c.id);
  assert(actionIds.includes("action:recon-checklist"));
  assert(actionIds.includes("action:latest-receipts"));
  assert(actionIds.includes("action:provider-health"));
  assert(actionIds.includes("action:refresh-all"));
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
  console.log(`${passed} command registry tests passed`);
}

run();
