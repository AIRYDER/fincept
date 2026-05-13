import type { PortfolioAllocationResult } from "../portfolioBuilder.types";
import type {
  StressGuardrailBreach,
  StressRegime,
  StressResult,
} from "./warRoomTypes";

export function evaluateStressGuardrails(
  allocation: PortfolioAllocationResult,
  result: Omit<StressResult, "guardrailBreaches">,
  regime: StressRegime,
): StressGuardrailBreach[] {
  const breaches: StressGuardrailBreach[] = [];
  if (result.pnlDeltaPct <= -18) {
    breaches.push({
      id: "drawdown",
      severity: "critical",
      message: `Estimated drawdown is ${result.pnlDeltaPct.toFixed(1)}%, beyond the critical scenario threshold.`,
    });
  } else if (result.pnlDeltaPct <= -8) {
    breaches.push({
      id: "drawdown",
      severity: "warn",
      message: `Estimated drawdown is ${result.pnlDeltaPct.toFixed(1)}%, above the warning threshold.`,
    });
  }

  const totalLoss = Math.abs(result.holdings.filter((holding) => holding.pnlDelta < 0).reduce((sum, holding) => sum + holding.pnlDelta, 0));
  const largestLoss = result.worstContributors[0];
  if (totalLoss > 0 && largestLoss && Math.abs(largestLoss.pnlDelta) / totalLoss > 0.35) {
    breaches.push({
      id: "single_name",
      severity: "warn",
      message: `${largestLoss.ticker} contributes more than 35% of estimated scenario losses.`,
    });
  }

  if (allocation.summary.largestSectorExposurePct > 40 && result.pnlDelta < 0) {
    breaches.push({
      id: "sector",
      severity: "warn",
      message: `${allocation.summary.largestSector ?? "Largest sector"} exposure is ${allocation.summary.largestSectorExposurePct.toFixed(1)}% during a losing scenario.`,
    });
  }

  if (regime.liquidityHaircutPct > 0 && allocation.summary.stockPct > allocation.summary.etfPct) {
    breaches.push({
      id: "liquidity",
      severity: "warn",
      message: "Single-name stock exposure is larger than ETF exposure during a liquidity-haircut regime.",
    });
  }

  if (regime.id === "melt_up" && allocation.summary.cashPercent > 10) {
    breaches.push({
      id: "cash_drag",
      severity: "info",
      message: `Cash is ${allocation.summary.cashPercent.toFixed(1)}%, creating opportunity cost in a melt-up scenario.`,
    });
  }

  return breaches;
}
