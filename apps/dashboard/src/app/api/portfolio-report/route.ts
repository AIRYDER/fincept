import { NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";

import { fallbackReport, sanitizeReportResponse } from "@/features/portfolio-builder/portfolioReportSchema";
import type {
  PortfolioAllocationResult,
  PortfolioModelProvider,
} from "@/features/portfolio-builder/portfolioBuilder.types";

export const runtime = "nodejs";

interface RequestBody {
  allocation?: PortfolioAllocationResult;
  provider?: PortfolioModelProvider;
}

const OPENAI_MODEL = process.env.OPENAI_PORTFOLIO_MODEL ?? "gpt-5.5";
const ANTHROPIC_MODEL = process.env.ANTHROPIC_PORTFOLIO_MODEL ?? "claude-opus-4-7";
const OPENAI_REASONING_EFFORT = process.env.OPENAI_PORTFOLIO_REASONING_EFFORT ?? "high";
const ANTHROPIC_THINKING_EFFORT = process.env.ANTHROPIC_PORTFOLIO_THINKING_EFFORT ?? "high";
const PORTFOLIO_REPORT_MAX_OUTPUT_TOKENS = Number(
  process.env.PORTFOLIO_REPORT_MAX_OUTPUT_TOKENS ?? "6000",
);
// TASK-0204: LLM calls can take 20-60 s for a long reasoning report. Cap at
// 90 s so a stuck provider falls through to the deterministic fallback
// instead of hanging the operator's browser indefinitely.
const LLM_TIMEOUT_MS = Number(process.env.PORTFOLIO_REPORT_TIMEOUT_MS ?? "90_000");

export async function POST(request: Request) {
  let body: RequestBody;
  try {
    body = (await request.json()) as RequestBody;
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (!body.allocation || !Array.isArray(body.allocation.holdings)) {
    return NextResponse.json({ error: "Missing allocation" }, { status: 400 });
  }

  const provider = body.provider ?? "auto";
  const allocation = body.allocation;
  const candidates = providerOrder(provider);
  const diagnostics: string[] = [];
  const openAiKey = getEnvSecret("OPENAI_API_KEY", ["FINCEPT_OPENAI_API_KEY"]);
  const anthropicKey = getEnvSecret("ANTHROPIC_API_KEY", ["FINCEPT_ANTHROPIC_API_KEY"]);
  for (const candidate of candidates) {
    try {
      if (candidate === "openai" && openAiKey) {
        const report = await callOpenAI(allocation, openAiKey);
        return NextResponse.json({
          providerLabel: `OpenAI ${OPENAI_MODEL} (${OPENAI_REASONING_EFFORT} reasoning)`,
          report: sanitizeReportResponse(
            report,
            allocation,
            `OpenAI ${OPENAI_MODEL} (${OPENAI_REASONING_EFFORT} reasoning)`,
          ),
        });
      }
      if (candidate === "openai") {
        diagnostics.push("OpenAI key was not available as OPENAI_API_KEY or FINCEPT_OPENAI_API_KEY.");
      }
      if (candidate === "anthropic" && anthropicKey) {
        const report = await callAnthropic(allocation, anthropicKey);
        return NextResponse.json({
          providerLabel: `Anthropic ${ANTHROPIC_MODEL} (${ANTHROPIC_THINKING_EFFORT} adaptive thinking)`,
          report: sanitizeReportResponse(
            report,
            allocation,
            `Anthropic ${ANTHROPIC_MODEL} (${ANTHROPIC_THINKING_EFFORT} adaptive thinking)`,
          ),
        });
      }
      if (candidate === "anthropic") {
        diagnostics.push("Anthropic key was not available as ANTHROPIC_API_KEY or FINCEPT_ANTHROPIC_API_KEY.");
      }
    } catch {
      diagnostics.push(`${candidate} report call failed or returned non-JSON output.`);
      continue;
    }
  }

  return NextResponse.json({
    providerLabel: "Local deterministic fallback",
    providerDiagnostics: diagnostics,
    report: fallbackReport(allocation, "Local deterministic fallback", diagnostics),
  });
}

function providerOrder(provider: PortfolioModelProvider): Array<"openai" | "anthropic"> {
  if (provider === "openai") return ["openai"];
  if (provider === "anthropic") return ["anthropic"];
  return ["openai", "anthropic"];
}

function promptFor(allocation: PortfolioAllocationResult): string {
  return JSON.stringify(
    {
      instruction:
        "Generate a professional investment committee report from your own analysis of the provided deterministic allocation, user preferences, full scored candidate universe, rejected alternatives, market snapshot, risk metrics, constraints, and assumptions. You are not just summarizing the chosen holdings: first audit whether the selected portfolio is the best implementable portfolio under the user inputs and the supplied universe. Consider every candidate in candidateAudit and candidateUniverse whose sector, theme, risk, horizon, liquidity, and income profile fits the user parameters. Explain close omissions and opportunity costs. You must not change, invent, or recalculate prices, shares, dollars, weights, ticker list, cash, or percentages in the locked allocation. Treat summary.cashReserve as the intentional user-selected reserve and summary.roundingCash as residual whole-share conversion cash; never describe roundingCash or totalCash as a deliberate reserve when cashReserve is 0. If a better portfolio would require live data, more securities, fractional shares, different constraints, or a broader universe, say that clearly instead of pretending certainty. Return only valid JSON with keys: executiveSummary, portfolioReasoning, optimalityReview, universeReview, holdingRationales, timeHorizonExplanation, riskAnalysis, rebalancingPlan, researchMandate, agentDebate, assumptionsAndLimitations.",
      style:
        "Write like a senior portfolio strategist, sector analyst, and risk officer preparing a serious investment committee packet. Be specific, analytical, and candid. Favor depth over speed. Avoid generic prose. State what won, what lost, why, and what would need to be checked with live data before trusting the portfolio.",
      requiredDepth: {
        executiveSummary: "One dense paragraph with objective, allocation posture, key risks, and confidence.",
        portfolioReasoning: "Several paragraphs covering capital deployment logic, risk budget, diversification, sector/theme tradeoffs, ETF versus stock mix, intentional cash-reserve policy, and residual whole-share rounding cash.",
        optimalityReview: "A direct review of whether this is the best portfolio under the user inputs. Reference selected holdings, close rejected candidates, constraints, and any changes that would improve the result.",
        universeReview: "Explain how the full candidate universe was considered. Mention eligible count, selected count, selected sector/theme filters, and notable omissions.",
        holdingRationales: "One ticker-specific rationale for every holding. Mention portfolio role and key risk.",
        timeHorizonExplanation: "Explain how the selected horizon changed concentration, volatility, ETF preference, intentional cash-reserve preference, and rebalancing cadence.",
        riskAnalysis: "Cover concentration, sector, volatility, drawdown, liquidity, macro, single-name, and AI/data uncertainty.",
        rebalancingPlan: "Give a practical monitoring and rebalance framework.",
        researchMandate: "A list of concrete data checks the model would run next: valuation, balance sheet, earnings revisions, momentum, liquidity, correlation, factor exposure, and sector breadth.",
        agentDebate: "Four to six short records with {agent, finding}, using agent roles like Universe Scout, Sector Analyst, Risk Officer, Tax/Liquidity Officer, and CIO.",
        assumptionsAndLimitations: "List explicit limitations, including no trade execution and demo data when applicable.",
      },
      input: allocation.input,
      summary: allocation.summary,
      cashBreakdown: {
        intentionalCashReserveDollars: allocation.summary.cashReserve,
        wholeShareRoundingCashDollars: allocation.summary.roundingCash,
        totalUninvestedCashDollars: allocation.summary.totalCash,
        totalUninvestedCashPct: allocation.summary.cashPercent,
        cashReserveWasExplicitlyZero: allocation.input.preferences.cashReservePct === 0,
      },
      candidateAudit: allocation.candidateAudit,
      candidateUniverse: allocation.candidateAudit.topSelected
        .concat(allocation.candidateAudit.topRejected)
        .map((candidate) => ({
          ticker: candidate.ticker,
          name: candidate.name,
          sector: candidate.sector,
          theme: candidate.theme,
          assetType: candidate.assetType,
          score: candidate.score,
          riskScore: candidate.riskScore,
          volatility: candidate.volatility,
          dividendScore: candidate.dividendScore,
          selected: candidate.selected,
          reason: candidate.reason,
        })),
      fullCandidateUniverse: allocation.candidateDiagnostics.map((candidate) => ({
        ticker: candidate.ticker,
        name: candidate.name,
        sector: candidate.sector,
        theme: candidate.theme,
        assetType: candidate.assetType,
        price: candidate.price,
        riskScore: candidate.riskScore,
        beta: candidate.beta,
        dividendScore: candidate.dividendScore,
        liquidityScore: candidate.liquidityScore,
        volatility: candidate.volatility,
        role: candidate.role,
        keyRisk: candidate.keyRisk,
      })),
      holdings: allocation.holdings.map((holding) => ({
        ticker: holding.ticker,
        name: holding.name,
        sector: holding.sector,
        assetType: holding.assetType,
        price: holding.price,
        dollarAllocation: holding.dollarAllocation,
        percentAllocation: holding.percentAllocation,
        shares: holding.shares,
        role: holding.role,
        keyRisk: holding.keyRisk,
      })),
      marketData: allocation.marketData,
      riskAnalysis: allocation.riskAnalysis,
      optimization: allocation.optimization,
      constraintsUsed: allocation.constraintsUsed,
      assumptions: allocation.assumptions,
    },
    null,
    2,
  );
}

async function callOpenAI(allocation: PortfolioAllocationResult, apiKey: string): Promise<unknown> {
  const response = await fetchWithTimeout("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: OPENAI_MODEL,
      reasoning: {
        effort: OPENAI_REASONING_EFFORT,
        summary: "auto",
      },
      max_output_tokens: PORTFOLIO_REPORT_MAX_OUTPUT_TOKENS,
      input: [
        {
          role: "developer",
          content:
            "You are a senior portfolio strategist and security-selection auditor. Audit the full supplied candidate universe against the user's instructions before writing. Never alter locked math. Return only valid JSON.",
        },
        { role: "user", content: promptFor(allocation) },
      ],
    }),
  });
  if (!response.ok) throw new Error("OpenAI report failed");
  const json = await response.json();
  const content = extractOpenAIResponseText(json);
  if (typeof content !== "string") throw new Error("OpenAI report missing content");
  return JSON.parse(content);
}

async function callAnthropic(allocation: PortfolioAllocationResult, apiKey: string): Promise<unknown> {
  const response = await fetchWithTimeout("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: ANTHROPIC_MODEL,
      max_tokens: PORTFOLIO_REPORT_MAX_OUTPUT_TOKENS,
      thinking: {
        type: "adaptive",
        effort: ANTHROPIC_THINKING_EFFORT,
        display: "summarized",
      },
      system: "You are a senior portfolio strategist and security-selection auditor. Audit the full supplied candidate universe against the user's instructions before writing. Never alter locked math. Return only valid JSON.",
      messages: [{ role: "user", content: promptFor(allocation) }],
    }),
  });
  if (!response.ok) throw new Error("Anthropic report failed");
  const json = await response.json();
  const text = json?.content?.find?.((item: { type?: string }) => item.type === "text")?.text;
  if (typeof text !== "string") throw new Error("Anthropic report missing content");
  return JSON.parse(text);
}

/**
 * TASK-0204: fetch wrapper with AbortController timeout. A stuck LLM provider
 * throws "LLM call timed out" instead of hanging the route indefinitely; the
 * caller's catch block then falls through to the next provider or the
 * deterministic fallback.
 */
async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number = LLM_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(`LLM call timed out after ${timeoutMs} ms`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

function extractOpenAIResponseText(json: unknown): string | null {
  const record = json as { output_text?: unknown; output?: unknown };
  if (typeof record.output_text === "string") return record.output_text;
  if (!Array.isArray(record.output)) return null;
  const chunks: string[] = [];
  for (const item of record.output) {
    const outputItem = item as { type?: string; content?: unknown };
    if (!Array.isArray(outputItem.content)) continue;
    for (const content of outputItem.content) {
      const block = content as { type?: string; text?: unknown };
      if (
        (block.type === "output_text" || block.type === "text") &&
        typeof block.text === "string"
      ) {
        chunks.push(block.text);
      }
    }
  }
  return chunks.length ? chunks.join("") : null;
}

function getEnvSecret(name: string, aliases: string[] = []): string | null {
  for (const key of [name, ...aliases]) {
    const value = process.env[key];
    if (value?.trim()) return value.trim();
  }
  for (const file of envFiles()) {
    const value = readEnvFileValue(file, [name, ...aliases]);
    if (value) return value;
  }
  return null;
}

function envFiles(): string[] {
  return [
    path.join(process.cwd(), ".env.local"),
    path.join(process.cwd(), ".env"),
    path.resolve(process.cwd(), "..", "..", ".env"),
  ];
}

function readEnvFileValue(file: string, names: string[]): string | null {
  try {
    if (!fs.existsSync(file)) return null;
    const text = fs.readFileSync(file, "utf8");
    for (const name of names) {
      const pattern = new RegExp(`^${escapeRegExp(name)}=(.*)$`, "m");
      const match = text.match(pattern);
      const value = match?.[1]?.trim().replace(/^['"]|['"]$/g, "");
      if (value) return value;
    }
  } catch {
    return null;
  }
  return null;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
