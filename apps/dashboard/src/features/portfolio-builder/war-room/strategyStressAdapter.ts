export interface UnavailableStrategyStressSubject {
  subjectType: "strategy";
  strategyId: string;
  available: false;
  warnings: string[];
}

export function buildUnavailableStrategyStressSubject(
  strategyId: string,
): UnavailableStrategyStressSubject {
  return {
    subjectType: "strategy",
    strategyId,
    available: false,
    warnings: [
      "Scenario War Room needs a read-only strategy exposure snapshot before strategy stress is available.",
      "No strategy lifecycle, OMS, broker, or order routes were called.",
    ],
  };
}
