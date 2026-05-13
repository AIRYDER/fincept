/**
 * command-registry — typed command definitions for the Universal Command Palette 2.0.
 *
 * Defines command categories, safety levels, and entity search integration.
 * Dangerous commands never execute directly — they route to confirmation surfaces.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type CommandCategory = "navigate" | "search" | "action" | "dangerous";
export type CommandSafety = "safe" | "confirm" | "readonly";

export interface PaletteCommand {
  id: string;
  label: string;
  category: CommandCategory;
  safety: CommandSafety;
  /** Mnemonic shortcut (e.g. "OV", "PS") */
  mnemonic?: string;
  /** Icon name from lucide-react */
  icon: string;
  /** Where to navigate on select. Dangerous commands route to confirmation page. */
  href: string;
  /** Keywords for fuzzy matching */
  keywords: string[];
  /** Group heading in the palette */
  group: string;
}

export interface EntitySearchResult {
  id: string;
  label: string;
  type: "symbol" | "strategy" | "model";
  href: string;
  keywords: string[];
}

// ---------------------------------------------------------------------------
// Static command registry
// ---------------------------------------------------------------------------

export const COMMANDS: PaletteCommand[] = [
  // Navigate — from NAV_ITEMS
  { id: "nav:overview", label: "Overview", category: "navigate", safety: "safe", mnemonic: "OV", icon: "LayoutDashboard", href: "/", keywords: ["home", "dashboard", "overview"], group: "Navigate" },
  { id: "nav:positions", label: "Positions", category: "navigate", safety: "safe", mnemonic: "PS", icon: "Briefcase", href: "/positions", keywords: ["positions", "portfolio", "holdings"], group: "Navigate" },
  { id: "nav:orders", label: "Orders", category: "navigate", safety: "safe", mnemonic: "OR", icon: "ScrollText", href: "/orders", keywords: ["orders", "trades", "fills"], group: "Navigate" },
  { id: "nav:recon", label: "Reconciliation", category: "navigate", safety: "safe", mnemonic: "RC", icon: "GitCompareArrows", href: "/reconciliation", keywords: ["recon", "reconciliation", "checklist"], group: "Navigate" },
  { id: "nav:strategies", label: "Strategies", category: "navigate", safety: "safe", mnemonic: "ST", icon: "Bot", href: "/strategies", keywords: ["strategies", "agents", "bots"], group: "Navigate" },
  { id: "nav:optimizer", label: "Optimizer", category: "navigate", safety: "safe", mnemonic: "PO", icon: "PieChart", href: "/portfolio-builder", keywords: ["optimizer", "portfolio", "builder"], group: "Navigate" },
  { id: "nav:news", label: "News", category: "navigate", safety: "safe", mnemonic: "NW", icon: "Newspaper", href: "/news", keywords: ["news", "headlines"], group: "Navigate" },
  { id: "nav:research", label: "Research", category: "navigate", safety: "safe", mnemonic: "RX", icon: "Microscope", href: "/research", keywords: ["research", "data", "provider"], group: "Navigate" },
  { id: "nav:news-lab", label: "News Lab", category: "navigate", safety: "safe", mnemonic: "NL", icon: "FlaskConical", href: "/news-impact-lab", keywords: ["news", "lab", "impact"], group: "Navigate" },
  { id: "nav:predictions", label: "Predictions", category: "navigate", safety: "safe", mnemonic: "PR", icon: "Activity", href: "/predictions", keywords: ["predictions", "signals", "forecast"], group: "Navigate" },
  { id: "nav:markets", label: "Markets", category: "navigate", safety: "safe", mnemonic: "MK", icon: "BarChart3", href: "/markets", keywords: ["markets", "prices", "quotes"], group: "Navigate" },
  { id: "nav:backtest", label: "Backtest", category: "navigate", safety: "safe", mnemonic: "BT", icon: "FlaskConical", href: "/backtest", keywords: ["backtest", "scenario", "lab"], group: "Navigate" },
  { id: "nav:models", label: "Models", category: "navigate", safety: "safe", mnemonic: "ML", icon: "Brain", href: "/models", keywords: ["models", "ai", "ml"], group: "Navigate" },
  { id: "nav:receipts", label: "Receipts", category: "navigate", safety: "safe", mnemonic: "PF", icon: "FileJson", href: "/receipts", keywords: ["receipts", "proof", "audit"], group: "Navigate" },
  { id: "nav:risk", label: "Risk", category: "navigate", safety: "safe", mnemonic: "RK", icon: "ShieldAlert", href: "/risk", keywords: ["risk", "kill", "switch"], group: "Navigate" },

  // Search — entity lookups
  { id: "search:symbol", label: "Search symbol…", category: "search", safety: "readonly", icon: "Search", href: "/markets?search=", keywords: ["symbol", "ticker", "find", "lookup"], group: "Search" },
  { id: "search:strategy", label: "Search strategy…", category: "search", safety: "readonly", icon: "Search", href: "/strategies?search=", keywords: ["strategy", "agent", "find", "lookup"], group: "Search" },
  { id: "search:model", label: "Search model…", category: "search", safety: "readonly", icon: "Search", href: "/models?search=", keywords: ["model", "ai", "find", "lookup"], group: "Search" },

  // Actions — safe / read-only
  { id: "action:recon-checklist", label: "Run reconciliation checklist", category: "action", safety: "readonly", icon: "CheckSquare", href: "/reconciliation", keywords: ["recon", "checklist", "audit", "check"], group: "Actions" },
  { id: "action:latest-receipts", label: "Open latest receipts", category: "action", safety: "readonly", icon: "FileJson", href: "/receipts", keywords: ["receipts", "latest", "proof"], group: "Actions" },
  { id: "action:provider-health", label: "Open provider health", category: "action", safety: "readonly", icon: "HeartPulse", href: "/research", keywords: ["provider", "health", "data", "source"], group: "Actions" },
  { id: "action:source-health", label: "Open source health", category: "action", safety: "readonly", icon: "Database", href: "/markets", keywords: ["source", "health", "coverage"], group: "Actions" },
  { id: "action:refresh-all", label: "Refresh all data", category: "action", safety: "safe", icon: "RefreshCw", href: "#refresh", keywords: ["refresh", "reload", "update"], group: "Actions" },
  { id: "action:backtest-lab", label: "Open backtest lab", category: "action", safety: "readonly", icon: "FlaskConical", href: "/backtest", keywords: ["backtest", "scenario", "lab", "attribution"], group: "Actions" },
  { id: "action:news-intel", label: "Open news intelligence", category: "action", safety: "readonly", icon: "Newspaper", href: "/news", keywords: ["news", "intelligence", "impact"], group: "Actions" },

  // Dangerous — route to confirmation, never execute directly
  { id: "dangerous:kill-switch", label: "Kill switch", category: "dangerous", safety: "confirm", mnemonic: "KS", icon: "Power", href: "/risk?action=kill", keywords: ["kill", "switch", "emergency", "halt"], group: "Dangerous" },
  { id: "dangerous:start-strategy", label: "Start strategy…", category: "dangerous", safety: "confirm", icon: "Play", href: "/strategies?action=start", keywords: ["start", "strategy", "agent", "run"], group: "Dangerous" },
  { id: "dangerous:stop-strategy", label: "Stop strategy…", category: "dangerous", safety: "confirm", icon: "Square", href: "/strategies?action=stop", keywords: ["stop", "strategy", "agent", "halt"], group: "Dangerous" },
  { id: "dangerous:promote-model", label: "Promote model…", category: "dangerous", safety: "confirm", icon: "ArrowUpCircle", href: "/models?action=promote", keywords: ["promote", "model", "upgrade", "production"], group: "Dangerous" },
  { id: "dangerous:place-order", label: "Place order…", category: "dangerous", safety: "confirm", icon: "Send", href: "/orders?action=new", keywords: ["place", "order", "buy", "sell", "trade"], group: "Dangerous" },
];

// ---------------------------------------------------------------------------
// Search helpers
// ---------------------------------------------------------------------------

export function filterCommands(query: string): PaletteCommand[] {
  if (!query.trim()) return COMMANDS;
  const q = query.toLowerCase();
  return COMMANDS.filter((cmd) => {
    if (cmd.label.toLowerCase().includes(q)) return true;
    if (cmd.mnemonic?.toLowerCase().startsWith(q)) return true;
    if (cmd.keywords.some((k) => k.includes(q))) return true;
    return false;
  });
}

export function buildEntityResults(
  symbols: string[],
  strategyIds: string[],
  modelIds: string[],
): EntitySearchResult[] {
  const results: EntitySearchResult[] = [];

  for (const symbol of symbols) {
    results.push({
      id: `entity:symbol:${symbol}`,
      label: symbol,
      type: "symbol",
      href: `/markets?symbol=${encodeURIComponent(symbol)}`,
      keywords: [symbol.toLowerCase(), "symbol", "ticker"],
    });
  }

  for (const strategyId of strategyIds) {
    results.push({
      id: `entity:strategy:${strategyId}`,
      label: strategyId,
      type: "strategy",
      href: `/strategies?strategy_id=${encodeURIComponent(strategyId)}`,
      keywords: [strategyId.toLowerCase(), "strategy", "agent"],
    });
  }

  for (const modelId of modelIds) {
    results.push({
      id: `entity:model:${modelId}`,
      label: modelId,
      type: "model",
      href: `/models?model_id=${encodeURIComponent(modelId)}`,
      keywords: [modelId.toLowerCase(), "model", "ai"],
    });
  }

  return results;
}

export function filterEntities(
  entities: EntitySearchResult[],
  query: string,
): EntitySearchResult[] {
  if (!query.trim()) return [];
  const q = query.toLowerCase();
  return entities.filter((e) => {
    if (e.label.toLowerCase().includes(q)) return true;
    if (e.keywords.some((k) => k.includes(q))) return true;
    return false;
  });
}
