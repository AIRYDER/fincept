import type { PortfolioAllocationResult } from "../portfolioBuilder.types";
import type {
  StressRegimeId,
  StressResult,
  StressSeverity,
} from "./warRoomTypes";

export interface WarRoomReceipt {
  kind: "scenario_war_room_receipt";
  generatedAt: string;
  subjectType: "portfolio";
  allocationMethod: string;
  dataMode: string;
  regimeId: StressRegimeId;
  severity: StressSeverity;
  inputHash: string;
  constraintsUsed: string[];
  warnings: string[];
  result: StressResult;
}

export function buildWarRoomReceipt(
  allocation: PortfolioAllocationResult,
  result: StressResult,
): WarRoomReceipt {
  const warnings = Array.from(new Set([...allocation.warnings, ...result.warnings]));
  const hashInput = {
    input: allocation.input,
    holdings: allocation.holdings.map((holding) => ({
      ticker: holding.ticker,
      dollarAllocation: holding.dollarAllocation,
      percentAllocation: holding.percentAllocation,
      sector: holding.sector,
      assetType: holding.assetType,
    })),
    regimeId: result.regimeId,
    severity: result.severity,
    constraintsUsed: allocation.constraintsUsed,
  };
  return {
    kind: "scenario_war_room_receipt",
    generatedAt: new Date().toISOString(),
    subjectType: "portfolio",
    allocationMethod: allocation.optimization.method,
    dataMode: allocation.marketData.dataMode,
    regimeId: result.regimeId,
    severity: result.severity,
    inputHash: hashStable(stableStringify(hashInput)),
    constraintsUsed: allocation.constraintsUsed,
    warnings,
    result,
  };
}

export function warRoomReceiptToJson(receipt: WarRoomReceipt): string {
  return JSON.stringify(receipt, null, 2);
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
    .join(",")}}`;
}

function hashStable(input: string): string {
  let hash = 2166136261;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `wr-${(hash >>> 0).toString(16).padStart(8, "0")}-${input.length.toString(16)}`;
}
