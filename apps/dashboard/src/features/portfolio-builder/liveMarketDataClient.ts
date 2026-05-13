import type { MarketDataSnapshot } from "./portfolioBuilder.types";

export interface MarketDataProvider {
  getSnapshot(): Promise<MarketDataSnapshot>;
}

export class UnavailableLiveMarketDataProvider implements MarketDataProvider {
  async getSnapshot(): Promise<MarketDataSnapshot> {
    return {
      dataMode: "live",
      timestamp: new Date().toISOString(),
      source: "Live market data adapter boundary",
      candidates: [],
      warnings: [
        "Live market data is not wired to portfolio-builder yet; use demo mode or connect /data coverage first.",
      ],
    };
  }
}
