import type { PortfolioAllocationResult } from "./portfolioBuilder.types";

const PDF_PRINT_MODE = "portfolio-pdf";

export function portfolioToJson(result: PortfolioAllocationResult): string {
  return JSON.stringify(result, null, 2);
}

export function portfolioToCsv(result: PortfolioAllocationResult): string {
  const headers = [
    "Ticker",
    "Name",
    "Sector",
    "Theme",
    "Asset Type",
    "Price",
    "Dollar Allocation",
    "Percent Allocation",
    "Shares",
    "Fractional",
    "Risk Rating",
    "Role",
    "Reason",
    "Key Risk",
  ];
  const rows = result.holdings.map((holding) => [
    holding.ticker,
    holding.name,
    holding.sector,
    holding.theme,
    holding.assetType,
    holding.price,
    holding.dollarAllocation,
    holding.percentAllocation,
    holding.shares,
    holding.fractional ? "yes" : "no",
    holding.riskRating,
    holding.role,
    holding.reason,
    holding.keyRisk,
  ]);
  return [headers, ...rows]
    .map((row) => row.map((cell) => escapeCsv(String(cell))).join(","))
    .join("\n");
}

export function downloadTextFile(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function buildPortfolioPdfFilename(result: PortfolioAllocationResult): string {
  const amount = Math.max(0, Math.round(result.summary.startingAmount));
  const risk = slugify(result.input.riskLevel);
  const horizon = slugify(result.input.horizon);
  return `fincept-portfolio-${amount}-${risk}-${horizon}.pdf`;
}

export function openPortfolioPdfPrintDialog(result: PortfolioAllocationResult) {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return;
  }

  const previousTitle = document.title;
  const previousHtmlMode = document.documentElement.dataset.printMode;
  const previousBodyMode = document.body.dataset.printMode;
  const filename = buildPortfolioPdfFilename(result);
  const title = filename.replace(/\.pdf$/i, "");
  let fallbackRestore: number | undefined;

  const restorePrintState = () => {
    document.title = previousTitle;
    restoreDatasetValue(document.documentElement.dataset, "printMode", previousHtmlMode);
    restoreDatasetValue(document.body.dataset, "printMode", previousBodyMode);
    if (fallbackRestore !== undefined) {
      window.clearTimeout(fallbackRestore);
    }
  };

  document.title = title;
  document.documentElement.dataset.printMode = PDF_PRINT_MODE;
  document.body.dataset.printMode = PDF_PRINT_MODE;
  window.addEventListener("afterprint", restorePrintState, { once: true });
  fallbackRestore = window.setTimeout(restorePrintState, 30000);
  window.setTimeout(() => window.print(), 50);
}

function escapeCsv(value: string): string {
  if (!/[",\n\r]/.test(value)) return value;
  return `"${value.replace(/"/g, '""')}"`;
}

function slugify(value: string): string {
  const slug = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "portfolio";
}

function restoreDatasetValue(
  dataset: DOMStringMap,
  key: keyof DOMStringMap,
  previousValue: string | undefined,
) {
  if (previousValue === undefined) {
    delete dataset[key];
    return;
  }
  dataset[key] = previousValue;
}
