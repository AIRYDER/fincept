import type {
  OptimizationConstraintSet,
  ReturnInput,
  WeightSolverResult,
} from "./optimizerTypes";

export function solveConstrainedWeights(
  proposedWeightsPct: number[],
  inputs: ReturnInput[],
  constraints: OptimizationConstraintSet,
): WeightSolverResult {
  const warnings: string[] = [];
  const bindingConstraints = new Set<string>();
  const investablePct = clamp(100 - constraints.cashReservePct, 0, 100);
  if (!inputs.length || investablePct <= 0) {
    return {
      weights: inputs.map(() => 0),
      totalWeightPct: 0,
      feasible: false,
      diagnostics: {
        feasible: false,
        iterations: 0,
        bindingConstraints: [],
        warnings: ["No investable universe or cash reserve consumed all capital."],
      },
    };
  }

  const normalized = normalize(proposedWeightsPct, investablePct, inputs.length);
  let weights = normalized.map((weight) => {
    if (weight > constraints.maxAllocationPerHoldingPct) bindingConstraints.add("max_holding");
    return Math.min(weight, constraints.maxAllocationPerHoldingPct);
  });

  weights = applySectorCaps(weights, inputs, constraints.maxSectorConcentrationPct, bindingConstraints);
  weights = redistribute(weights, inputs, constraints, investablePct, bindingConstraints);

  weights = weights.map((weight) => {
    if (weight > 0 && weight < constraints.minAllocationPerHoldingPct) {
      bindingConstraints.add("min_holding");
      return 0;
    }
    return weight;
  });
  weights = redistribute(weights, inputs, constraints, investablePct, bindingConstraints);

  const totalWeightPct = round(weights.reduce((sum, weight) => sum + weight, 0));
  const feasible = totalWeightPct > 0 && totalWeightPct <= investablePct + 0.01;
  if (totalWeightPct < investablePct - 0.5) {
    warnings.push("Constraints left residual cash beyond the requested reserve.");
  }
  return {
    weights: weights.map(round),
    totalWeightPct,
    feasible,
    diagnostics: {
      feasible,
      iterations: 8,
      bindingConstraints: Array.from(bindingConstraints).sort(),
      warnings,
    },
  };
}

function applySectorCaps(
  weights: number[],
  inputs: ReturnInput[],
  sectorCapPct: number,
  bindingConstraints: Set<string>,
): number[] {
  const sectorTotals = new Map<string, number>();
  return weights.map((weight, index) => {
    const sector = inputs[index].sector;
    const used = sectorTotals.get(sector) ?? 0;
    const next = Math.min(weight, Math.max(0, sectorCapPct - used));
    if (next < weight) bindingConstraints.add("max_sector");
    sectorTotals.set(sector, used + next);
    return next;
  });
}

function redistribute(
  weights: number[],
  inputs: ReturnInput[],
  constraints: OptimizationConstraintSet,
  investablePct: number,
  bindingConstraints: Set<string>,
): number[] {
  let next = [...weights];
  for (let pass = 0; pass < 8; pass += 1) {
    const total = next.reduce((sum, weight) => sum + weight, 0);
    const residual = investablePct - total;
    if (residual <= 0.01) break;
    const sectorTotals = new Map<string, number>();
    inputs.forEach((input, index) => {
      sectorTotals.set(input.sector, (sectorTotals.get(input.sector) ?? 0) + next[index]);
    });
    const eligible = next
      .map((weight, index) => ({ index, weight, input: inputs[index] }))
      .filter(({ weight, input }) => {
        const sectorRoom = constraints.maxSectorConcentrationPct - (sectorTotals.get(input.sector) ?? 0);
        return weight > 0 && weight < constraints.maxAllocationPerHoldingPct - 0.01 && sectorRoom > 0.01;
      });
    if (!eligible.length) break;
    const increment = residual / eligible.length;
    for (const item of eligible) {
      const sectorUsed = sectorTotals.get(item.input.sector) ?? 0;
      const add = Math.min(
        increment,
        constraints.maxAllocationPerHoldingPct - next[item.index],
        constraints.maxSectorConcentrationPct - sectorUsed,
      );
      if (add <= 0) continue;
      next[item.index] += add;
      sectorTotals.set(item.input.sector, sectorUsed + add);
    }
  }
  if (next.reduce((sum, weight) => sum + weight, 0) < investablePct - 0.5) {
    bindingConstraints.add("residual_cash");
  }
  return next;
}

function normalize(values: number[], targetTotal: number, length: number): number[] {
  const cleaned = Array.from({ length }, (_, index) => Math.max(0, finite(values[index], 0)));
  const total = cleaned.reduce((sum, value) => sum + value, 0);
  if (total <= 0) return cleaned.map(() => targetTotal / Math.max(1, length));
  return cleaned.map((value) => (value / total) * targetTotal);
}

function finite(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, finite(value, min)));
}

function round(value: number): number {
  return Math.round((Number.isFinite(value) ? value : 0) * 10000) / 10000;
}
