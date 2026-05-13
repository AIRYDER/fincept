import assert from "assert";

import {
  buildSystemReadinessPacket,
  OPTIONAL_ENV_VARS,
  POWERSHELL_COMMANDS,
  REQUIRED_ENV_VARS,
  type SystemReadinessInput,
} from "./system-readiness";

type TestFn = () => void | Promise<void>;
const tests: Array<{ name: string; fn: TestFn }> = [];
function test(name: string, fn: TestFn) {
  tests.push({ name, fn });
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function baseInput(overrides: Partial<SystemReadinessInput> = {}): SystemReadinessInput {
  return {
    servicesData: {
      services: [
        { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
        { name: "ingestor", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
        { name: "features", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
      ],
      summary: { up: 3, expected: 3, stale_after_sec: 30, ttl_sec: 120 },
    },
    servicesError: false,
    killSwitch: { engaged: false, actor: null, reason: null, alert_id: null, ts_unix: null },
    openbb: { ok: true, url: "http://127.0.0.1:6900" },
    apiUrl: "http://127.0.0.1:8010",
    envVarPresence: Object.fromEntries(
      [...REQUIRED_ENV_VARS, ...OPTIONAL_ENV_VARS].map((v) => [v.name, true]),
    ),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Schema / static catalog tests
// ---------------------------------------------------------------------------

test("REQUIRED_ENV_VARS contains FINCEPT_API_URL and REDIS_URL", () => {
  const names = REQUIRED_ENV_VARS.map((v) => v.name);
  assert(names.includes("FINCEPT_API_URL"));
  assert(names.includes("REDIS_URL"));
});

test("OPTIONAL_ENV_VARS does not duplicate required vars", () => {
  const required = new Set(REQUIRED_ENV_VARS.map((v) => v.name));
  for (const v of OPTIONAL_ENV_VARS) {
    assert(!required.has(v.name), `${v.name} should not be in both required and optional`);
  }
});

test("POWERSHELL_COMMANDS use Windows PowerShell defaults", () => {
  for (const cmd of POWERSHELL_COMMANDS) {
    assert(cmd.id);
    assert(cmd.label);
    assert(cmd.description);
    assert(cmd.command);
    assert(typeof cmd.safe === "boolean");
  }
  // start/stop scripts should use .\ PowerShell prefix
  const startCmd = POWERSHELL_COMMANDS.find((c) => c.id === "start-all");
  assert(startCmd);
  assert(startCmd.command.startsWith(".\\scripts\\"), `start-all should use Windows .\\scripts\\ prefix, got ${startCmd.command}`);
});

test("POWERSHELL_COMMANDS marks mutating commands as unsafe", () => {
  const startAll = POWERSHELL_COMMANDS.find((c) => c.id === "start-all");
  const stopAll = POWERSHELL_COMMANDS.find((c) => c.id === "stop-all");
  const alembic = POWERSHELL_COMMANDS.find((c) => c.id === "alembic-upgrade");
  assert(startAll?.safe === false);
  assert(stopAll?.safe === false);
  assert(alembic?.safe === false);
});

test("POWERSHELL_COMMANDS marks read-only commands as safe", () => {
  const status = POWERSHELL_COMMANDS.find((c) => c.id === "status");
  const paperSpine = POWERSHELL_COMMANDS.find((c) => c.id === "paper-spine");
  const routeSmoke = POWERSHELL_COMMANDS.find((c) => c.id === "route-smoke");
  assert(status?.safe === true);
  assert(paperSpine?.safe === true);
  assert(routeSmoke?.safe === true);
});

test("POWERSHELL_COMMANDS includes proof scripts visible from roadmap", () => {
  const ids = POWERSHELL_COMMANDS.map((c) => c.id);
  assert(ids.includes("paper-spine"));
  assert(ids.includes("openbb-proof"));
  assert(ids.includes("route-smoke"));
});

// ---------------------------------------------------------------------------
// Analyzer tests
// ---------------------------------------------------------------------------

test("ready state when everything is up and present", () => {
  const packet = buildSystemReadinessPacket(baseInput());
  assert.equal(packet.state, "ready");
  assert(packet.score > 80, `expected score > 80, got ${packet.score}`);
});

test("blocked state when API is unreachable", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({ servicesData: null, servicesError: true }),
  );
  assert.equal(packet.state, "blocked");
  assert.equal(packet.api.reachable, false);
});

test("blocked state when required env vars are missing", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      envVarPresence: { FINCEPT_API_URL: false, REDIS_URL: false },
    }),
  );
  assert.equal(packet.state, "blocked");
  const envCheck = packet.checks.find((c) => c.id === "env-vars");
  assert.equal(envCheck?.state, "blocked");
});

test("blocked state when kill switch is engaged", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      killSwitch: { engaged: true, actor: "operator", reason: "test halt", alert_id: "x", ts_unix: 1 },
    }),
  );
  assert.equal(packet.state, "blocked");
  assert.equal(packet.killSwitch.state, "engaged");
});

test("review state when services are stale", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      servicesData: {
        services: [
          { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "ingestor", status: "stale", last_beat_unix: 1, age_sec: 60, expected: true },
        ],
        summary: { up: 1, expected: 2, stale_after_sec: 30, ttl_sec: 120 },
      },
    }),
  );
  assert.equal(packet.state, "review");
});

test("blocked state when expected services are down", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      servicesData: {
        services: [
          { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "ingestor", status: "down", last_beat_unix: null, age_sec: null, expected: true },
        ],
        summary: { up: 1, expected: 2, stale_after_sec: 30, ttl_sec: 120 },
      },
    }),
  );
  assert.equal(packet.state, "blocked");
});

test("review state when OpenBB is down (optional dependency)", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({ openbb: { ok: false, url: "http://127.0.0.1:6900", error: "ECONNREFUSED" } }),
  );
  // OpenBB down is review, not blocked (paper trading doesn't require OpenBB)
  assert(packet.state === "review" || packet.state === "ready");
  assert.equal(packet.openbb.state, "down");
});

test("service summary counts up/stale/down correctly", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      servicesData: {
        services: [
          { name: "a", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "b", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "c", status: "stale", last_beat_unix: 1, age_sec: 60, expected: true },
          { name: "d", status: "down", last_beat_unix: null, age_sec: null, expected: true },
        ],
        summary: { up: 2, expected: 4, stale_after_sec: 30, ttl_sec: 120 },
      },
    }),
  );
  assert.equal(packet.serviceSummary.up, 2);
  assert.equal(packet.serviceSummary.stale, 1);
  assert.equal(packet.serviceSummary.down, 1);
  assert.equal(packet.serviceSummary.expected, 4);
});

test("rogue (non-expected) services are not counted toward expected summary", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      servicesData: {
        services: [
          { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "rogue", status: "up", last_beat_unix: 1, age_sec: 1, expected: false },
        ],
        summary: { up: 2, expected: 1, stale_after_sec: 30, ttl_sec: 120 },
      },
    }),
  );
  assert.equal(packet.serviceSummary.expected, 1);
  assert.equal(packet.serviceSummary.total, 2);
});

test("env vars list contains both required and optional", () => {
  const packet = buildSystemReadinessPacket(baseInput());
  const required = packet.envVars.filter((v) => v.required);
  const optional = packet.envVars.filter((v) => !v.required);
  assert.equal(required.length, REQUIRED_ENV_VARS.length);
  assert.equal(optional.length, OPTIONAL_ENV_VARS.length);
});

// ---------------------------------------------------------------------------
// Acceptance criteria tests (roadmap #15)
// ---------------------------------------------------------------------------

test("acceptance: env vars expose names only, never values", () => {
  // The analyzer only accepts presence booleans, never values
  const packet = buildSystemReadinessPacket(baseInput());
  for (const env of packet.envVars) {
    // Output shape must not include any "value" field
    assert(!("value" in env), `EnvVarSpec for ${env.name} leaks value field`);
    assert(typeof env.present === "boolean");
    assert(typeof env.name === "string");
  }
});

test("acceptance: copyable commands use Windows PowerShell defaults", () => {
  const start = POWERSHELL_COMMANDS.find((c) => c.id === "start-all");
  const status = POWERSHELL_COMMANDS.find((c) => c.id === "status");
  const stop = POWERSHELL_COMMANDS.find((c) => c.id === "stop-all");
  assert(start?.command.includes(".\\"));
  assert(status?.command.includes(".\\"));
  assert(stop?.command.includes(".\\"));
});

test("acceptance: route smoke and proof status are visible", () => {
  const ids = POWERSHELL_COMMANDS.map((c) => c.id);
  assert(ids.includes("route-smoke"));
  assert(ids.includes("paper-spine"));
  assert(ids.includes("openbb-proof"));
  // Receipts state is exposed
  const packet = buildSystemReadinessPacket(baseInput());
  assert(packet.receipts.state);
  assert(typeof packet.receipts.total === "number");
});

test("acceptance: new users see exactly what is running and what is missing", () => {
  const packet = buildSystemReadinessPacket(
    baseInput({
      servicesData: {
        services: [
          { name: "api", status: "up", last_beat_unix: 1, age_sec: 1, expected: true },
          { name: "ingestor", status: "down", last_beat_unix: null, age_sec: null, expected: true },
        ],
        summary: { up: 1, expected: 2, stale_after_sec: 30, ttl_sec: 120 },
      },
      envVarPresence: { FINCEPT_API_URL: true, REDIS_URL: false },
    }),
  );
  // Headline mentions counts
  const servicesCheck = packet.checks.find((c) => c.id === "services");
  assert(servicesCheck?.detail.includes("up"));
  assert(servicesCheck?.detail.includes("down"));
  // Env check names the missing var
  const envCheck = packet.checks.find((c) => c.id === "env-vars");
  assert(envCheck?.detail.includes("REDIS_URL"));
});

test("score is bounded 0-100", () => {
  const ready = buildSystemReadinessPacket(baseInput());
  const blocked = buildSystemReadinessPacket(
    baseInput({
      servicesData: null,
      servicesError: true,
      killSwitch: { engaged: true, actor: null, reason: null, alert_id: null, ts_unix: null },
      openbb: { ok: false, url: "x" },
      envVarPresence: {},
    }),
  );
  assert(ready.score >= 0 && ready.score <= 100);
  assert(blocked.score >= 0 && blocked.score <= 100);
  assert(ready.score > blocked.score);
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
  console.log(`${passed} system readiness tests passed`);
}

run();
