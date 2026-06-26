/**
 * Mock data discipline.
 *
 * Any data that comes from a mock/seed/demo source — not yet wired to a
 * real API — must:
 *   1. Be wrapped in a `withMockFlag()` call so the MOCK badge is
 *      available wherever the data is rendered.
 *   2. Show a `<MockBadge />` chip on the panel that consumes it.
 *   3. Log a `MOCK:` warning in dev so the team notices.
 *
 * The reason this is enforced: dad (the operator) needs to trust the
 * dashboard.  A panel that says "API LIVE" while showing fake numbers
 * collapses that trust.  MOCK is unmistakable.
 */

export type MockFlag = {
  /** Human-readable source label, e.g. "Seed demo", "Inline fixture". */
  source: string;
  /** Optional ticket / issue link, e.g. "FIN-1234". */
  ticket?: string;
};

const warned = new Set<string>();

/** Wrap a value in a mock-flag.  Logs a dev-only warning once per source. */
export function withMockFlag<T>(value: T, flag: MockFlag): T & { __mock: MockFlag } {
  if (typeof window !== "undefined" && process.env.NODE_ENV !== "production") {
    const key = `${flag.source}:${flag.ticket ?? ""}`;
    if (!warned.has(key)) {
      // eslint-disable-next-line no-console
      console.warn(
        `MOCK: rendering mock data from "${flag.source}"${
          flag.ticket ? ` — see ${flag.ticket}` : ""
        }`,
      );
      warned.add(key);
    }
  }
  return Object.assign(value as object, { __mock: flag }) as T & {
    __mock: MockFlag;
  };
}

/** Type guard: was this value flagged as mock? */
export function isMock<T>(value: T | (T & { __mock?: MockFlag })): value is T & {
  __mock: MockFlag;
} {
  return (
    typeof value === "object" &&
    value !== null &&
    "__mock" in (value as object) &&
    !!(value as { __mock?: unknown }).__mock
  );
}

/**
 * Seeded pseudo-random generator (Mulberry32) — gives us deterministic
 * "live-looking" mock data so refreshes don't reshuffle the same widget.
 */
export function seededRandom(seed: number): () => number {
  let s = seed >>> 0;
  return function () {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Deterministic walk: returns `count` price points that trend gently. */
export function mockPriceWalk(opts: {
  seed: number;
  count: number;
  start?: number;
  volatility?: number;
  drift?: number;
}): { x: number; y: number }[] {
  const { seed, count, start = 100, volatility = 0.015, drift = 0.0005 } = opts;
  const rand = seededRandom(seed);
  const out: { x: number; y: number }[] = [];
  let p = start;
  for (let i = 0; i < count; i++) {
    const r = (rand() - 0.5) * 2; // -1..+1
    p = p * (1 + r * volatility + drift);
    out.push({ x: i, y: p });
  }
  return out;
}

/** Deterministic volume series that loosely correlates with the price walk. */
export function mockVolumeWalk(opts: {
  seed: number;
  count: number;
  baseVolume?: number;
  volatility?: number;
}): { x: number; y: number }[] {
  const { seed, count, baseVolume = 1_000_000, volatility = 0.4 } = opts;
  const rand = seededRandom(seed + 7919);
  const out: { x: number; y: number }[] = [];
  for (let i = 0; i < count; i++) {
    const r = rand() - 0.5;
    const v = Math.max(0, baseVolume * (1 + r * volatility));
    out.push({ x: i, y: v });
  }
  return out;
}
