/**
 * Per-class quick-start presets for the strategy params editor.
 *
 * Each preset is a one-click bundle of ``params`` that covers a
 * common use case for that strategy class — "default", "conservative",
 * "aggressive", and a paper-account "test" preset that uses tiny
 * notional so an operator can dry-run safely.
 *
 * Adding a new strategy class
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~
 *
 * Drop a new entry in ``PRESETS`` keyed by the registry name (the
 * same string you'd pass to ``class_name`` on a StrategyConfig).
 * Presets are intentionally a dashboard-only concept; the server
 * doesn't validate them — the strategy host validates the merged
 * params dict at instantiation time.
 *
 * Tone tagging
 * ~~~~~~~~~~~~
 *
 * ``tone`` controls the chip colour:
 *   - ``"primary"``  -- the recommended starting point
 *   - ``"long"``     -- conservative / capital-preserving
 *   - ``"short"``    -- aggressive / higher risk
 *   - ``"muted"``    -- sandbox / test
 */

export type PresetTone = "primary" | "long" | "short" | "muted";

export interface ParamsPreset {
  /** Short label for the button (3-4 words max). */
  label: string;
  /** One-line description shown on hover and below the button row. */
  description: string;
  /** The params dict to merge into the form on click. */
  params: Record<string, unknown>;
  /** Visual tone bucket. */
  tone: PresetTone;
}

export const PRESETS: Record<string, ParamsPreset[]> = {
  buy_and_hold: [
    {
      label: "Default $10k",
      description:
        "Open one $10k notional long per symbol on the first bar.  Floor for any strategy benchmark.",
      params: { per_symbol_notional: 10000 },
      tone: "primary",
    },
    {
      label: "Conservative $1k",
      description:
        "$1k notional per symbol — useful for sizing the long tail of a many-symbol basket.",
      params: { per_symbol_notional: 1000 },
      tone: "long",
    },
    {
      label: "Aggressive $100k",
      description:
        "$100k per symbol.  Verify your risk caps allow the resulting gross notional.",
      params: { per_symbol_notional: 100000 },
      tone: "short",
    },
    {
      label: "Sandbox $250",
      description:
        "Tiny notional for end-to-end dry-runs on the paper account.",
      params: { per_symbol_notional: 250 },
      tone: "muted",
    },
  ],

  ma_crossover: [
    {
      label: "Default 5/20",
      description:
        "Classic SMA(5) vs SMA(20) crossover, $10k per symbol.  The textbook starting point.",
      params: { fast: 5, slow: 20, per_symbol_notional: 10000 },
      tone: "primary",
    },
    {
      label: "Slow trend 10/50",
      description:
        "Slower windows (10/50) for higher-conviction trend-following.  Fewer trades, lower slippage.",
      params: { fast: 10, slow: 50, per_symbol_notional: 10000 },
      tone: "long",
    },
    {
      label: "Fast scalp 3/12",
      description:
        "Very short windows for intraday-style flips.  Costs eat into edge fast.",
      params: { fast: 3, slow: 12, per_symbol_notional: 10000 },
      tone: "short",
    },
    {
      label: "Long-term 20/100",
      description:
        "Long-horizon trend follow.  Holds positions for many bars; great for a model-light baseline.",
      params: { fast: 20, slow: 100, per_symbol_notional: 10000 },
      tone: "muted",
    },
  ],

  gbm: [
    {
      label: "Default 0/0",
      description:
        "Trade every prediction the model emits (entry=0, exit=0).  Highest signal volume.",
      params: {
        entry_threshold: 0,
        exit_threshold: 0,
        per_symbol_notional: 10000,
        bar_minutes: 1,
      },
      tone: "primary",
    },
    {
      label: "Conservative ±0.10",
      description:
        "Only act on |prediction| ≥ 0.10.  Filters out near-coin-flip signals.",
      params: {
        entry_threshold: 0.1,
        exit_threshold: 0.1,
        per_symbol_notional: 5000,
        bar_minutes: 1,
      },
      tone: "long",
    },
    {
      label: "Aggressive ±0.05",
      description:
        "Lower threshold (0.05) on a $20k stake — more trades, more carry.",
      params: {
        entry_threshold: 0.05,
        exit_threshold: 0.05,
        per_symbol_notional: 20000,
        bar_minutes: 1,
      },
      tone: "short",
    },
    {
      label: "Sandbox ±0.20",
      description:
        "High threshold + tiny notional for sanity-checking a freshly-trained model.",
      params: {
        entry_threshold: 0.2,
        exit_threshold: 0.2,
        per_symbol_notional: 500,
        bar_minutes: 1,
      },
      tone: "muted",
    },
  ],
};

/** Look up presets for a class name; returns ``[]`` if unknown so the
 * UI can simply hide the preset row. */
export function getPresets(className: string | null | undefined): ParamsPreset[] {
  if (!className) return [];
  return PRESETS[className] ?? [];
}
