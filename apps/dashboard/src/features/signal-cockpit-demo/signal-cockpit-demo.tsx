"use client";

import { motion } from "framer-motion";
import {
  ArrowLeft,
  CircleDot,
  Database,
  Gauge,
  Layers3,
  Radar,
  ScanSearch,
  ShieldAlert,
  Sparkles,
  TriangleAlert,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { cn } from "@/lib/utils";

import styles from "./signal-cockpit-demo.module.css";

type LayerFilter = "all" | "symbol" | "news" | "model" | "provider" | "risk";
type DrawerTab = "summary" | "evidence" | "payload" | "trace";

type NodeKind = "symbol" | "news" | "model" | "provider" | "risk";
type NodeState = "verified" | "healthy" | "partial" | "warning";
type NodeTone = "cyan" | "blue" | "violet" | "amber";

interface CockpitNode {
  id: string;
  kind: NodeKind;
  label: string;
  shortLabel: string;
  group: string;
  x: number;
  y: number;
  size: "lg" | "md" | "sm";
  tone: NodeTone;
  state: NodeState;
  confidence: string;
  detail: string;
  summary: string;
  evidence: string[];
  payload: string;
  trace: string[];
}

interface CockpitEdge {
  id: string;
  from: string;
  to: string;
  tone: NodeTone;
  style: "solid" | "dashed" | "dotted";
}

interface TimelineEvent {
  id: string;
  timestamp: string;
  symbol: string;
  lane: "MKT" | "NWS" | "COV" | "MDL";
  label: string;
  tone: NodeTone;
  nodeId: string;
}

interface SourceHealth {
  name: string;
  mode: string;
  freshness: string;
  coverage: string;
  confidence: string;
  status: NodeState;
}

interface Suggestion {
  id: string;
  label: string;
  description: string;
  onSelect: () => void;
}

const NODES: CockpitNode[] = [
  {
    id: "symbol-aapl",
    kind: "symbol",
    label: "AAPL",
    shortLabel: "AAPL",
    group: "Focus",
    x: 18,
    y: 24,
    size: "lg",
    tone: "cyan",
    state: "verified",
    confidence: "0.78",
    detail: "Active symbol node · contiguous 1Min bars sampled",
    summary:
      "AAPL has the cleanest evidence chain in the current window, with fresh bars, recent headlines, and model caveats still visible.",
    evidence: [
      "47 sampled bars available from Alpaca IEX with no immediate coverage gap.",
      "3 fresh Alpaca headlines detected in the last 20 minutes.",
      "GBM artifact generated with medium confidence and no direct execution path.",
    ],
    payload: `{
  "ticker": "AAPL",
  "barsSampled": 47,
  "latestBar": "09:59:00",
  "newsFreshness": "4m ago",
  "gbmConfidence": 0.62,
  "status": "verified"
}`,
    trace: [
      "alpaca-bars.fetch ............. 182ms",
      "alpaca-news.scan .............. 241ms",
      "gbm-placeholder.run ........... 323ms",
      "signal-graph.project .......... 44ms",
    ],
  },
  {
    id: "symbol-nvda",
    kind: "symbol",
    label: "NVDA",
    shortLabel: "NVDA",
    group: "Focus",
    x: 17,
    y: 62,
    size: "lg",
    tone: "cyan",
    state: "partial",
    confidence: "0.51",
    detail: "Active symbol node · partial bar coverage",
    summary:
      "NVDA remains in focus but its sampled 1Min evidence chain is incomplete, so downstream model interpretation is cautionary.",
    evidence: [
      "Coverage gap detected in the sampled 10:00-10:02 window.",
      "News density remains high, but bar continuity is partial.",
      "Alpha artifact is present and should be read as provisional only.",
    ],
    payload: `{
  "ticker": "NVDA",
  "barsSampled": 12,
  "coverageGap": true,
  "gapWindow": "10:00-10:02",
  "alphaConfidence": 0.48,
  "status": "partial"
}`,
    trace: [
      "alpaca-bars.fetch ............. 191ms",
      "coverage-gap.detect ........... 27ms",
      "news-alpha-placeholder.run .... 278ms",
      "operator-rail.compose ......... 41ms",
    ],
  },
  {
    id: "news-alpaca",
    kind: "news",
    label: "ALPACA HEADLINES",
    shortLabel: "NEWS",
    group: "Evidence",
    x: 39,
    y: 27,
    size: "md",
    tone: "blue",
    state: "healthy",
    confidence: "MED",
    detail: "Fresh headline cluster",
    summary:
      "The Alpaca news lane is fresh and dense enough to anchor context, but it does not overrule coverage issues in price data.",
    evidence: [
      "3 AAPL headlines and 5 NVDA headlines in the active review window.",
      "Freshest headline detected at 09:58.",
      "Topics cluster around AI chips, analysts, and product demand.",
    ],
    payload: `{
  "source": "alpaca-news",
  "headlines": 8,
  "freshest": "09:58",
  "topics": ["ai chips", "analyst commentary", "data center demand"]
}`,
    trace: [
      "alpaca-news.fetch ............. 241ms",
      "headline-cluster.score ........ 33ms",
      "evidence-stack.append ......... 18ms",
    ],
  },
  {
    id: "provider-alpaca",
    kind: "provider",
    label: "ALPACA IEX",
    shortLabel: "ALPACA",
    group: "Sources",
    x: 40,
    y: 56,
    size: "md",
    tone: "cyan",
    state: "healthy",
    confidence: "MED",
    detail: "Primary market data provider",
    summary:
      "Alpaca IEX is healthy and recent, but its free-feed coverage remains partial in this mock window.",
    evidence: [
      "Freshness cadence is current.",
      "Coverage is partial by design for the seeded demo state.",
      "Source remains the canonical first hop for bars and headlines.",
    ],
    payload: `{
  "provider": "Alpaca IEX",
  "mode": "IEX free feed",
  "freshness": "recent",
  "coverage": "partial",
  "status": "healthy"
}`,
    trace: [
      "provider-health.sample ........ 12ms",
      "coverage-audit.run ............ 16ms",
      "source-led.update ............. 9ms",
    ],
  },
  {
    id: "provider-openbb",
    kind: "provider",
    label: "OPENBB FUNDAMENTALS",
    shortLabel: "OPENBB",
    group: "Sources",
    x: 43,
    y: 79,
    size: "sm",
    tone: "blue",
    state: "partial",
    confidence: "UNK",
    detail: "Secondary research provider",
    summary:
      "OpenBB is available as a research fallback, but it is intentionally represented as provider-dependent rather than guaranteed.",
    evidence: [
      "Useful for quote and fundamentals spot checks.",
      "Not the primary evidence lane for this cockpit state.",
      "Remains a suggested next check, not an always-on dependency.",
    ],
    payload: `{
  "provider": "OpenBB",
  "mode": "manual research lane",
  "availability": "provider-dependent",
  "status": "partial"
}`,
    trace: [
      "fallback-provider.ping ........ 22ms",
      "quote-lane.defer .............. 8ms",
    ],
  },
  {
    id: "model-gbm",
    kind: "model",
    label: "GBM",
    shortLabel: "GBM",
    group: "Model",
    x: 62,
    y: 17,
    size: "md",
    tone: "violet",
    state: "partial",
    confidence: "0.62",
    detail: "Placeholder model artifact",
    summary:
      "The GBM lane is present as a caveated artifact. It explains model posture without implying actionability.",
    evidence: [
      "Medium confidence ring in the clean AAPL path.",
      "Confidence should degrade if evidence coverage weakens.",
      "No execution language or routing is exposed.",
    ],
    payload: `{
  "model": "gbm.v1",
  "confidence": 0.62,
  "status": "generated_with_caveat",
  "executable": false
}`,
    trace: [
      "feature-snapshot.load ......... 103ms",
      "gbm-placeholder.run ........... 323ms",
      "confidence-ring.paint ......... 6ms",
    ],
  },
  {
    id: "model-alpha",
    kind: "model",
    label: "ALPHA PRED",
    shortLabel: "ALPHA",
    group: "Model",
    x: 64,
    y: 63,
    size: "md",
    tone: "violet",
    state: "partial",
    confidence: "0.48",
    detail: "News alpha placeholder",
    summary:
      "Alpha prediction remains visible as an evidentiary node, but it is visually subordinate to the underlying coverage warning.",
    evidence: [
      "Low/medium confidence state.",
      "News activity contributes to visibility.",
      "Coverage gap prevents stronger interpretation.",
    ],
    payload: `{
  "model": "news_alpha.v0",
  "confidence": 0.48,
  "status": "placeholder_artifact",
  "coverageBound": true
}`,
    trace: [
      "headline-features.load ........ 88ms",
      "alpha-placeholder.run ......... 278ms",
      "risk-caveat.inject ............ 11ms",
    ],
  },
  {
    id: "risk-gap",
    kind: "risk",
    label: "COVERAGE GAP",
    shortLabel: "RISK",
    group: "Risk",
    x: 79,
    y: 33,
    size: "md",
    tone: "amber",
    state: "warning",
    confidence: "LOW/MED",
    detail: "Coverage warning node",
    summary:
      "The active caveat is a partial-data warning: no continuous 1Min bar chain was found for NVDA in the sampled window.",
    evidence: [
      "Gap detected between 10:00 and 10:02.",
      "Interpretation should remain read-only and caveat-first.",
      "Suggested next check is to expand the bar window.",
    ],
    payload: `{
  "warning": "coverage_gap",
  "symbol": "NVDA",
  "window": "10:00-10:02",
  "severity": "low_medium",
  "status": "warning"
}`,
    trace: [
      "coverage-gap.detect ........... 27ms",
      "operator-rail.warn ............ 12ms",
      "amber-rail.paint .............. 5ms",
    ],
  },
  {
    id: "strategy-align",
    kind: "risk",
    label: "STRATEGY ALIGN",
    shortLabel: "ALIGN",
    group: "Risk",
    x: 61,
    y: 44,
    size: "sm",
    tone: "amber",
    state: "partial",
    confidence: "CAVEAT",
    detail: "Strategy exposure placeholder",
    summary:
      "Strategy alignment remains a contextual caveat surface in this mock, not an execution module.",
    evidence: [
      "Current position or exposure detail is unavailable in the demo state.",
      "Visible to show where exposure reasoning would appear.",
      "Does not convert into order intent.",
    ],
    payload: `{
  "surface": "strategy_alignment",
  "positionState": "unavailable",
  "status": "caveat_only"
}`,
    trace: [
      "exposure-surface.seed ......... 15ms",
      "caveat-card.paint ............. 7ms",
    ],
  },
];

const EDGES: CockpitEdge[] = [
  { id: "e-aapl-news", from: "symbol-aapl", to: "news-alpaca", tone: "blue", style: "solid" },
  { id: "e-aapl-gbm", from: "symbol-aapl", to: "model-gbm", tone: "violet", style: "dashed" },
  { id: "e-aapl-provider", from: "symbol-aapl", to: "provider-alpaca", tone: "cyan", style: "solid" },
  { id: "e-nvda-news", from: "symbol-nvda", to: "news-alpaca", tone: "blue", style: "dashed" },
  { id: "e-nvda-alpha", from: "symbol-nvda", to: "model-alpha", tone: "violet", style: "dotted" },
  { id: "e-nvda-provider", from: "symbol-nvda", to: "provider-alpaca", tone: "cyan", style: "solid" },
  { id: "e-provider-openbb", from: "provider-alpaca", to: "provider-openbb", tone: "blue", style: "dotted" },
  { id: "e-gbm-risk", from: "model-gbm", to: "risk-gap", tone: "amber", style: "dashed" },
  { id: "e-alpha-risk", from: "model-alpha", to: "risk-gap", tone: "amber", style: "dotted" },
  { id: "e-nvda-risk", from: "symbol-nvda", to: "risk-gap", tone: "amber", style: "dashed" },
  { id: "e-aapl-align", from: "symbol-aapl", to: "strategy-align", tone: "amber", style: "dotted" },
  { id: "e-nvda-align", from: "symbol-nvda", to: "strategy-align", tone: "amber", style: "dotted" },
];

const TIMELINE: TimelineEvent[] = [
  {
    id: "ev-1",
    timestamp: "09:30",
    symbol: "AAPL",
    lane: "MKT",
    label: "Alpaca IEX · Open bar OK",
    tone: "cyan",
    nodeId: "symbol-aapl",
  },
  {
    id: "ev-2",
    timestamp: "09:44",
    symbol: "AAPL",
    lane: "NWS",
    label: "Alpaca · Headline detected",
    tone: "blue",
    nodeId: "news-alpaca",
  },
  {
    id: "ev-3",
    timestamp: "10:02",
    symbol: "NVDA",
    lane: "COV",
    label: "Alpaca IEX · Gap detected",
    tone: "amber",
    nodeId: "risk-gap",
  },
  {
    id: "ev-4",
    timestamp: "10:15",
    symbol: "NVDA",
    lane: "MDL",
    label: "Pipeline · Dev artifact generated",
    tone: "violet",
    nodeId: "model-alpha",
  },
];

const SOURCES: SourceHealth[] = [
  {
    name: "Alpaca IEX",
    mode: "IEX free feed",
    freshness: "recent",
    coverage: "partial",
    confidence: "medium",
    status: "healthy",
  },
  {
    name: "OpenBB",
    mode: "manual research lane",
    freshness: "on demand",
    coverage: "provider dependent",
    confidence: "unknown",
    status: "partial",
  },
  {
    name: "Models",
    mode: "placeholder artifacts",
    freshness: "current",
    coverage: "caveat first",
    confidence: "low/medium",
    status: "partial",
  },
];

const LAYER_ITEMS: { id: LayerFilter; label: string }[] = [
  { id: "all", label: "All layers" },
  { id: "symbol", label: "Symbols" },
  { id: "news", label: "News" },
  { id: "model", label: "Models" },
  { id: "provider", label: "Sources" },
  { id: "risk", label: "Risk" },
];

const DRAWER_TABS: { id: DrawerTab; label: string }[] = [
  { id: "summary", label: "Summary" },
  { id: "evidence", label: "Evidence" },
  { id: "payload", label: "Raw Payload" },
  { id: "trace", label: "API Trace" },
];

function isNodeVisible(node: CockpitNode, filter: LayerFilter) {
  return filter === "all" || node.kind === filter;
}

function stateLabel(state: NodeState) {
  switch (state) {
    case "verified":
      return "VERIFIED";
    case "healthy":
      return "HEALTHY";
    case "partial":
      return "PARTIAL";
    case "warning":
      return "WARNING";
    default:
      return "UNKNOWN";
  }
}

export function SignalCockpitDemo() {
  const [selectedNodeId, setSelectedNodeId] = useState<string>("risk-gap");
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [layerFilter, setLayerFilter] = useState<LayerFilter>("all");
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("summary");
  const [interval, setInterval] = useState("1MIN");
  const [focusSymbols] = useState(["AAPL", "NVDA"]);

  const selectedNode =
    NODES.find((node) => node.id === selectedNodeId) ?? NODES[0];

  const suggestions: Suggestion[] = [
    {
      id: "expand-window",
      label: "Expand bar window",
      description: "Switch interval to 5MIN to relax the sampled coverage window.",
      onSelect: () => {
        setInterval("5MIN");
        setSelectedNodeId("risk-gap");
        setDrawerTab("evidence");
      },
    },
    {
      id: "fetch-openbb",
      label: "Fetch OpenBB quote",
      description: "Pivot the inspector to the secondary research lane.",
      onSelect: () => {
        setSelectedNodeId("provider-openbb");
        setDrawerTab("summary");
      },
    },
    {
      id: "compare-sentiment",
      label: "Compare news sentiment",
      description: "Re-center on the headline cluster and the model lane it influences.",
      onSelect: () => {
        setSelectedNodeId("news-alpaca");
        setDrawerTab("evidence");
      },
    },
    {
      id: "inspect-payload",
      label: "Inspect raw payload",
      description: "Open the payload tab for the currently selected node.",
      onSelect: () => {
        setDrawerOpen(true);
        setDrawerTab("payload");
      },
    },
  ];

  const visibleNodes = useMemo(
    () => NODES.filter((node) => isNodeVisible(node, layerFilter)),
    [layerFilter],
  );

  const visibleNodeIds = useMemo(
    () => new Set(visibleNodes.map((node) => node.id)),
    [visibleNodes],
  );

  const highlightedNodeIds = useMemo(() => {
    const pivot = hoveredNodeId ?? selectedNodeId;
    const next = new Set<string>([pivot]);
    for (const edge of EDGES) {
      if (edge.from === pivot) {
        next.add(edge.to);
      }
      if (edge.to === pivot) {
        next.add(edge.from);
      }
    }
    return next;
  }, [hoveredNodeId, selectedNodeId]);

  const visibleEdges = useMemo(() => {
    return EDGES.filter(
      (edge) =>
        visibleNodeIds.has(edge.from) &&
        visibleNodeIds.has(edge.to),
    );
  }, [visibleNodeIds]);

  return (
    <div className={styles.page}>
      <div className={styles.chromeTop}>
        <div className={styles.demoMeta}>
          <Link href="/" className={styles.backLink}>
            <ArrowLeft className="h-3.5 w-3.5" />
            Current dashboard
          </Link>
          <div className={styles.demoNote}>
            <span className={styles.demoDot} />
            Mock route · seeded evidence · not wired to live services
          </div>
        </div>
        <div className={styles.demoMetaRight}>
          <div className={styles.metaGhost}>Merged source spec</div>
        </div>
      </div>

      <div className={styles.shell}>
        <div className={styles.focusBar}>
          <div className={styles.brandBlock}>
            <span className={styles.brandGlyph}>
              <span />
              <span />
            </span>
            <span className={styles.brandText}>FINCEPT</span>
          </div>
          <div className={styles.focusStrip}>
            <span className={styles.stripLabel}>FOCUS</span>
            <div className={styles.focusTokens}>
              {focusSymbols.map((symbol) => (
                <span key={symbol} className={styles.focusToken}>
                  {symbol}
                </span>
              ))}
            </div>
          </div>
          <div className={styles.focusStrip}>
            <span className={styles.stripLabel}>FEED</span>
            <span className={styles.stripValue}>ALPACA IEX</span>
          </div>
          <div className={styles.focusStrip}>
            <span className={styles.stripLabel}>INT</span>
            <div className={styles.intervalGroup}>
              {["1MIN", "5MIN", "1H"].map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setInterval(value)}
                  className={cn(
                    styles.intervalButton,
                    interval === value && styles.intervalButtonActive,
                  )}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>
          <button type="button" className={styles.pulseScan}>
            <ScanSearch className="h-4 w-4" />
            RUN PULSE
          </button>
        </div>

        <div className={styles.safetyBar}>
          <div className={styles.safetyPill}>
            <CircleDot className="h-3.5 w-3.5" />
            READ ONLY MODE VERIFIED
          </div>
          <div className={styles.safetyPill}>
            <ShieldAlert className="h-3.5 w-3.5" />
            NO ORDER PATH
          </div>
          <div className={styles.safetyPillMuted}>EXPERIMENTAL ARTIFACTS PRESENT</div>
        </div>

        <div className={styles.contentGrid}>
          <aside className={styles.leftRail}>
            <section className={styles.rackSection}>
              <div className={styles.sectionHeader}>
                <span>NAV RAIL</span>
                <span className={styles.sectionHeaderDim}>260PX</span>
              </div>
              <div className={styles.symbolUniverse}>
                {[
                  { symbol: "AAPL", state: "ACTIVE", tone: "cyan", confidence: "MED" },
                  { symbol: "NVDA", state: "ACTIVE", tone: "amber", confidence: "LOW/MED" },
                  { symbol: "MSFT", state: "IDLE", tone: "muted", confidence: "—" },
                  { symbol: "TSLA", state: "IDLE", tone: "muted", confidence: "—" },
                ].map((item) => (
                  <div key={item.symbol} className={styles.symbolRow}>
                    <div
                      className={cn(
                        styles.symbolDot,
                        item.tone === "cyan" && styles.symbolDotCyan,
                        item.tone === "amber" && styles.symbolDotAmber,
                      )}
                    />
                    <span className={styles.symbolName}>{item.symbol}</span>
                    <span className={styles.symbolMeta}>{item.state}</span>
                    <span className={styles.symbolMeta}>{item.confidence}</span>
                  </div>
                ))}
              </div>
            </section>

            <section className={styles.rackSection}>
              <div className={styles.sectionHeader}>
                <span>FOCUS SET</span>
                <span className={styles.sectionHeaderDim}>2</span>
              </div>
              <div className={styles.focusSet}>
                {focusSymbols.map((symbol) => (
                  <button
                    key={symbol}
                    type="button"
                    onClick={() =>
                      setSelectedNodeId(
                        symbol === "AAPL" ? "symbol-aapl" : "symbol-nvda",
                      )
                    }
                    className={cn(
                      styles.focusSetCard,
                      selectedNode.label === symbol && styles.focusSetCardActive,
                    )}
                  >
                    <div className={styles.focusSetLabel}>{symbol}</div>
                    <div className={styles.focusSetSub}>
                      {symbol === "AAPL" ? "Recent bars + headlines" : "Coverage caution"}
                    </div>
                  </button>
                ))}
              </div>
            </section>

            <section className={styles.rackSection}>
              <div className={styles.sectionHeader}>
                <span>SOURCE HEALTH</span>
                <span className={styles.sectionHeaderDim}>CARTRIDGES</span>
              </div>
              <div className={styles.sourceStack}>
                {SOURCES.map((source) => (
                  <div key={source.name} className={styles.sourceCard}>
                    <div className={styles.sourceTopRow}>
                      <span>{source.name}</span>
                      <span
                        className={cn(
                          styles.sourceState,
                          source.status === "healthy" && styles.sourceStateCyan,
                          source.status === "partial" && styles.sourceStateAmber,
                        )}
                      >
                        {stateLabel(source.status)}
                      </span>
                    </div>
                    <dl className={styles.sourceFacts}>
                      <div>
                        <dt>Mode</dt>
                        <dd>{source.mode}</dd>
                      </div>
                      <div>
                        <dt>Freshness</dt>
                        <dd>{source.freshness}</dd>
                      </div>
                      <div>
                        <dt>Coverage</dt>
                        <dd>{source.coverage}</dd>
                      </div>
                      <div>
                        <dt>Conf</dt>
                        <dd>{source.confidence}</dd>
                      </div>
                    </dl>
                  </div>
                ))}
              </div>
            </section>

            <section className={styles.rackSection}>
              <div className={styles.sectionHeader}>
                <span>LAYERS</span>
                <span className={styles.sectionHeaderDim}>FILTER</span>
              </div>
              <div className={styles.layerGrid}>
                {LAYER_ITEMS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setLayerFilter(item.id)}
                    className={cn(
                      styles.layerButton,
                      layerFilter === item.id && styles.layerButtonActive,
                    )}
                  >
                    <Layers3 className="h-3.5 w-3.5" />
                    {item.label}
                  </button>
                ))}
              </div>
            </section>
          </aside>

          <section className={styles.centerStage}>
            <div className={styles.graphPanel}>
              <div className={styles.panelHeader}>
                <div>
                  <span className={styles.panelLabel}>SIGNAL CONSTELLATION GRAPH</span>
                  <p className={styles.panelSub}>
                    Evidence is spatial, connected, and caveat-first.
                  </p>
                </div>
                <div className={styles.panelKpis}>
                  <div>
                    <span>FOCUS</span>
                    <strong>2</strong>
                  </div>
                  <div>
                    <span>LAYERS</span>
                    <strong>{layerFilter === "all" ? "ALL" : layerFilter.toUpperCase()}</strong>
                  </div>
                </div>
              </div>

              <div className={styles.graphStage}>
                <div className={styles.graphGrid} />
                <svg
                  viewBox="0 0 1000 620"
                  className={styles.edgeCanvas}
                  aria-hidden="true"
                >
                  {visibleEdges.map((edge) => {
                    const from = NODES.find((node) => node.id === edge.from);
                    const to = NODES.find((node) => node.id === edge.to);
                    if (!from || !to) {
                      return null;
                    }
                    const x1 = from.x * 10;
                    const y1 = from.y * 6.2;
                    const x2 = to.x * 10;
                    const y2 = to.y * 6.2;
                    const mx = (x1 + x2) / 2;
                    const my = (y1 + y2) / 2 - Math.abs(y1 - y2) * 0.14;
                    const active =
                      highlightedNodeIds.has(edge.from) &&
                      highlightedNodeIds.has(edge.to);
                    return (
                      <path
                        key={edge.id}
                        d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
                        className={cn(
                          styles.edge,
                          edge.tone === "cyan" && styles.edgeCyan,
                          edge.tone === "blue" && styles.edgeBlue,
                          edge.tone === "violet" && styles.edgeViolet,
                          edge.tone === "amber" && styles.edgeAmber,
                          edge.style === "dashed" && styles.edgeDashed,
                          edge.style === "dotted" && styles.edgeDotted,
                          active ? styles.edgeActive : styles.edgeMuted,
                        )}
                      />
                    );
                  })}
                </svg>

                {visibleNodes.map((node) => {
                  const isActive = selectedNodeId === node.id;
                  const isConnected = highlightedNodeIds.has(node.id);
                  return (
                    <motion.button
                      key={node.id}
                      type="button"
                      whileHover={{ y: -2, scale: 1.01 }}
                      onHoverStart={() => setHoveredNodeId(node.id)}
                      onHoverEnd={() => setHoveredNodeId(null)}
                      onClick={() => setSelectedNodeId(node.id)}
                      className={cn(
                        styles.node,
                        styles[`node${node.size.toUpperCase()}` as keyof typeof styles],
                        styles[`node${node.tone.charAt(0).toUpperCase()}${node.tone.slice(1)}` as keyof typeof styles],
                        node.state === "warning" && styles.nodeWarning,
                        isActive && styles.nodeActive,
                        isConnected && styles.nodeConnected,
                      )}
                      style={{
                        left: `${node.x}%`,
                        top: `${node.y}%`,
                      }}
                    >
                      <span className={styles.nodeFrame}>
                        <span className={styles.nodeType}>{node.group}</span>
                        <span className={styles.nodeLabel}>{node.shortLabel}</span>
                        <span className={styles.nodeMeta}>{node.confidence}</span>
                      </span>
                    </motion.button>
                  );
                })}

                <div className={styles.graphLegend}>
                  <span className={styles.legendItem}>
                    <span className={cn(styles.legendDot, styles.legendDotCyan)} />
                    verified freshness
                  </span>
                  <span className={styles.legendItem}>
                    <span className={cn(styles.legendDot, styles.legendDotBlue)} />
                    news pulse
                  </span>
                  <span className={styles.legendItem}>
                    <span className={cn(styles.legendDot, styles.legendDotViolet)} />
                    model confidence
                  </span>
                  <span className={styles.legendItem}>
                    <span className={cn(styles.legendDot, styles.legendDotAmber)} />
                    caveat state
                  </span>
                </div>
              </div>
            </div>

            <div className={styles.timelinePanel}>
              <div className={styles.panelHeader}>
                <div>
                  <span className={styles.panelLabel}>MARKET PULSE TIMELINE</span>
                  <p className={styles.panelSub}>
                    Receipt-printer chronology with direct graph binding.
                  </p>
                </div>
                <div className={styles.timelineRanges}>
                  {["1H", "24H"].map((range) => (
                    <button key={range} type="button" className={styles.rangeButton}>
                      {range}
                    </button>
                  ))}
                </div>
              </div>

              <div className={styles.timelineRows}>
                {TIMELINE.map((event) => (
                  <button
                    key={event.id}
                    type="button"
                    onClick={() => setSelectedNodeId(event.nodeId)}
                    className={cn(
                      styles.timelineRow,
                      selectedNodeId === event.nodeId && styles.timelineRowActive,
                    )}
                  >
                    <span className={styles.timelineTime}>{event.timestamp}</span>
                    <span
                      className={cn(
                        styles.timelineLane,
                        event.tone === "cyan" && styles.timelineLaneCyan,
                        event.tone === "blue" && styles.timelineLaneBlue,
                        event.tone === "violet" && styles.timelineLaneViolet,
                        event.tone === "amber" && styles.timelineLaneAmber,
                      )}
                    >
                      {event.lane}
                    </span>
                    <span className={styles.timelineSymbol}>{event.symbol}</span>
                    <span className={styles.timelineLabel}>{event.label}</span>
                  </button>
                ))}
              </div>
            </div>
          </section>

          <aside className={styles.rightRail}>
            <section className={styles.operatorRail}>
              <div className={styles.sectionHeader}>
                <span>AI OPERATOR RAIL</span>
                <span className={styles.sectionHeaderDim}>~380PX</span>
              </div>

              <div className={styles.operatorCard}>
                <div className={styles.operatorTitle}>
                  <Radar className="h-4 w-4" />
                  CURRENT FOCUS
                </div>
                <div className={styles.operatorBody}>
                  {focusSymbols.join(", ")} · Alpaca IEX · {interval}
                </div>
              </div>

              <div className={styles.operatorCard}>
                <div className={styles.operatorTitle}>
                  <Sparkles className="h-4 w-4" />
                  DETECTED
                </div>
                <ul className={styles.detectedList}>
                  <li>Fresh headlines remain visible for both symbols.</li>
                  <li>Recent bars are complete for AAPL but partial for NVDA.</li>
                  <li>Model artifacts are present and still subordinate to source health.</li>
                </ul>
              </div>

              <div className={cn(styles.operatorCard, styles.operatorCardInsight)}>
                <div className={styles.operatorTitle}>
                  <Workflow className="h-4 w-4" />
                  WHY IT MATTERS
                </div>
                <p className={styles.insightCopy}>
                  AAPL shows a contiguous evidence path, while NVDA carries an explicit
                  coverage caveat. The cockpit keeps the warning spatially attached to
                  the symbol and its model outputs so confidence never looks cleaner than
                  the source coverage actually is.
                </p>
              </div>

              <div className={styles.operatorCard}>
                <div className={styles.operatorTitle}>
                  <Gauge className="h-4 w-4" />
                  SUGGESTED NEXT CHECKS
                </div>
                <div className={styles.suggestionList}>
                  {suggestions.map((suggestion, index) => (
                    <button
                      key={suggestion.id}
                      type="button"
                      onClick={suggestion.onSelect}
                      className={styles.suggestionButton}
                    >
                      <span className={styles.suggestionIndex}>{index + 1}</span>
                      <span className={styles.suggestionText}>
                        <strong>{suggestion.label}</strong>
                        <span>{suggestion.description}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            </section>

            <section className={styles.evidenceStack}>
              <div className={styles.sectionHeader}>
                <span>EVIDENCE STACK</span>
                <span className={styles.sectionHeaderDim}>L1-L4</span>
              </div>
              <div className={styles.evidenceCards}>
                {[
                  {
                    key: "market-data",
                    label: "MKT DAT",
                    caption: "47 bars · last 09:59",
                    icon: Database,
                    tone: "cyan",
                  },
                  {
                    key: "news",
                    label: "NEWS",
                    caption: "8 headlines · freshest 09:58",
                    icon: Radar,
                    tone: "blue",
                  },
                  {
                    key: "models",
                    label: "MODELS",
                    caption: "GBM + alpha · caveated",
                    icon: Workflow,
                    tone: "violet",
                  },
                  {
                    key: "risk",
                    label: "RISK",
                    caption: "Coverage caveat attached",
                    icon: TriangleAlert,
                    tone: "amber",
                  },
                ].map((card) => {
                  const Icon = card.icon;
                  return (
                    <div key={card.key} className={styles.evidenceCard}>
                      <div
                        className={cn(
                          styles.evidenceIcon,
                          card.tone === "cyan" && styles.evidenceIconCyan,
                          card.tone === "blue" && styles.evidenceIconBlue,
                          card.tone === "violet" && styles.evidenceIconViolet,
                          card.tone === "amber" && styles.evidenceIconAmber,
                        )}
                      >
                        <Icon className="h-3.5 w-3.5" />
                      </div>
                      <div className={styles.evidenceMeta}>
                        <strong>{card.label}</strong>
                        <span>{card.caption}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>

            <section className={styles.inspectorPanel}>
              <div className={styles.sectionHeader}>
                <span>NODE INSPECTOR</span>
                <span className={styles.sectionHeaderDim}>{selectedNode.group}</span>
              </div>
              <div className={styles.inspectorCard}>
                <div className={styles.inspectorTop}>
                  <div>
                    <div className={styles.inspectorLabel}>{selectedNode.label}</div>
                    <div className={styles.inspectorSub}>{selectedNode.detail}</div>
                  </div>
                  <div
                    className={cn(
                      styles.inspectorState,
                      selectedNode.state === "verified" && styles.inspectorStateCyan,
                      selectedNode.state === "healthy" && styles.inspectorStateBlue,
                      selectedNode.state === "partial" && styles.inspectorStateViolet,
                      selectedNode.state === "warning" && styles.inspectorStateAmber,
                    )}
                  >
                    {stateLabel(selectedNode.state)}
                  </div>
                </div>
                <p className={styles.inspectorSummary}>{selectedNode.summary}</p>
                <dl className={styles.inspectorFacts}>
                  <div>
                    <dt>Type</dt>
                    <dd>{selectedNode.kind}</dd>
                  </div>
                  <div>
                    <dt>Confidence</dt>
                    <dd>{selectedNode.confidence}</dd>
                  </div>
                  <div>
                    <dt>Color lock</dt>
                    <dd>{selectedNode.tone}</dd>
                  </div>
                  <div>
                    <dt>Selection</dt>
                    <dd>Spatially linked</dd>
                  </div>
                </dl>
              </div>
            </section>
          </aside>
        </div>

        <motion.section
          layout
          className={cn(styles.drawer, drawerOpen && styles.drawerOpen)}
        >
          <div className={styles.drawerTop}>
            <div>
              <div className={styles.panelLabel}>SERVICE HATCH</div>
              <p className={styles.panelSub}>
                Progressive disclosure for the selected node.
              </p>
            </div>
            <div className={styles.drawerControls}>
              <div className={styles.drawerTabs}>
                {DRAWER_TABS.map((tab) => (
                  <button
                    key={tab.id}
                    type="button"
                    onClick={() => setDrawerTab(tab.id)}
                    className={cn(
                      styles.drawerTab,
                      drawerTab === tab.id && styles.drawerTabActive,
                    )}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={() => setDrawerOpen((open) => !open)}
                className={styles.drawerToggle}
              >
                {drawerOpen ? "Collapse" : "Expand"}
              </button>
            </div>
          </div>

          <div className={styles.drawerBody}>
            {drawerTab === "summary" ? (
              <div className={styles.drawerPane}>
                <h3>{selectedNode.label}</h3>
                <p>{selectedNode.summary}</p>
              </div>
            ) : null}

            {drawerTab === "evidence" ? (
              <div className={styles.drawerPane}>
                <h3>Evidence fragments</h3>
                <ul className={styles.drawerList}>
                  {selectedNode.evidence.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {drawerTab === "payload" ? (
              <div className={styles.drawerPane}>
                <h3>Raw payload</h3>
                <pre className={styles.codeBlock}>{selectedNode.payload}</pre>
              </div>
            ) : null}

            {drawerTab === "trace" ? (
              <div className={styles.drawerPane}>
                <h3>API trace</h3>
                <ul className={styles.traceList}>
                  {selectedNode.trace.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </motion.section>
      </div>
    </div>
  );
}
