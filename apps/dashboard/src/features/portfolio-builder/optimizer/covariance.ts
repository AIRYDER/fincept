import type { CovarianceMatrix, ReturnInput } from "./optimizerTypes";

export function buildCovarianceMatrix(inputs: ReturnInput[]): CovarianceMatrix {
  return {
    tickers: inputs.map((input) => input.ticker),
    values: inputs.map((row, rowIndex) =>
      inputs.map((column, columnIndex) => {
        const rowVol = row.annualVolatilityPct / 100;
        const columnVol = column.annualVolatilityPct / 100;
        if (rowIndex === columnIndex) return round(rowVol * rowVol);
        const correlation = row.sector === column.sector ? 0.58 : 0.22;
        return round(rowVol * columnVol * correlation);
      }),
    ),
  };
}

export function portfolioVolatilityPct(weightsPct: number[], covariance: CovarianceMatrix): number {
  const weights = weightsPct.map((weight) => weight / 100);
  let variance = 0;
  for (let row = 0; row < weights.length; row += 1) {
    for (let column = 0; column < weights.length; column += 1) {
      variance += weights[row] * weights[column] * (covariance.values[row]?.[column] ?? 0);
    }
  }
  return round(Math.sqrt(Math.max(0, variance)) * 100);
}

function round(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 1000000) / 1000000;
}
