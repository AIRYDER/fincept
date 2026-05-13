"use client";

import type { PortfolioHolding } from "./portfolioBuilder.types";
import { cn, formatNumber, formatUsd } from "@/lib/utils";

export function PortfolioAllocationTable({ holdings }: { holdings: PortfolioHolding[] }) {
  return (
    <div className="overflow-x-auto border border-border">
      <table className="w-full min-w-[980px] text-sm">
        <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
          <tr className="border-b border-border/70">
            <th className="px-3 py-2 text-left">Ticker</th>
            <th className="px-3 py-2 text-left">Name</th>
            <th className="px-3 py-2 text-left">Theme</th>
            <th className="px-3 py-2 text-left">Type</th>
            <th className="px-3 py-2 text-right">Price</th>
            <th className="px-3 py-2 text-right">Allocation</th>
            <th className="px-3 py-2 text-right">Weight</th>
            <th className="px-3 py-2 text-right">Shares</th>
            <th className="px-3 py-2 text-left">Risk</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((holding) => (
            <tr key={holding.ticker} className="border-b border-border/40 last:border-0">
              <td className="px-3 py-2 font-mono text-foreground">{holding.ticker}</td>
              <td className="px-3 py-2 text-muted-foreground">{holding.name}</td>
              <td className="px-3 py-2 text-muted-foreground">{holding.theme}</td>
              <td className="px-3 py-2">
                <span className="border border-border bg-card px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-cyan">
                  {holding.assetType}
                </span>
              </td>
              <td className="px-3 py-2 text-right font-mono">{formatUsd(holding.price)}</td>
              <td className="px-3 py-2 text-right font-mono">{formatUsd(holding.dollarAllocation)}</td>
              <td className="px-3 py-2 text-right font-mono">{holding.percentAllocation.toFixed(2)}%</td>
              <td className="px-3 py-2 text-right font-mono">
                {formatNumber(holding.shares, holding.fractional ? 4 : 0)}
              </td>
              <td className="px-3 py-2">
                <span
                  className={cn(
                    "border px-1.5 py-0.5 text-[10px] uppercase tracking-wider",
                    holding.riskRating === "Low" && "border-long/40 text-long",
                    holding.riskRating === "Medium" && "border-cyan/40 text-cyan",
                    holding.riskRating === "High" && "border-warn/40 text-warn",
                    holding.riskRating === "Speculative" && "border-short/40 text-short",
                  )}
                >
                  {holding.riskRating}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
