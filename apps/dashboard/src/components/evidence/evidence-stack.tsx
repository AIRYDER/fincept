"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

type EvidenceLevel = "summary" | "evidence" | "payload" | "trace";

type EvidenceTone = "verified" | "caveat" | "critical" | "model" | "muted";

export interface EvidenceRow {
  label: string;
  value: string;
  tone?: EvidenceTone;
}

export interface EvidenceStackProps {
  title: string;
  summary: string;
  evidence?: EvidenceRow[];
  payload?: unknown;
  trace?: EvidenceRow[];
  tone?: EvidenceTone;
  emptyLabel?: string;
}

const LEVELS: Array<{ id: EvidenceLevel; label: string }> = [
  { id: "summary", label: "L1 Summary" },
  { id: "evidence", label: "L2 Evidence" },
  { id: "payload", label: "L3 Payload" },
  { id: "trace", label: "L4 Trace" },
];

function toneClass(tone: EvidenceTone) {
  if (tone === "verified") return "border-cyan/40 text-cyan";
  if (tone === "caveat") return "border-warn/40 text-warn";
  if (tone === "critical") return "border-short/50 text-short";
  if (tone === "model") return "border-violet-400/50 text-violet-300";
  return "border-border text-muted-foreground";
}

function formatPayload(payload: unknown) {
  if (payload === null || payload === undefined || payload === "") return "insufficient evidence";
  if (typeof payload === "string") return payload;
  return JSON.stringify(payload, null, 2);
}

function EvidenceRows({ rows, emptyLabel }: { rows: EvidenceRow[] | undefined; emptyLabel: string }) {
  if (!rows?.length) {
    return <div className="text-xs text-muted-foreground">{emptyLabel}</div>;
  }
  return (
    <dl className="grid gap-2 md:grid-cols-2">
      {rows.map((row) => (
        <div key={`${row.label}-${row.value}`} className={cn("border p-2", toneClass(row.tone ?? "muted"))}>
          <dt className="text-[10px] uppercase tracking-wider text-muted-foreground">{row.label}</dt>
          <dd className="mt-1 break-words font-mono text-xs">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function EvidenceStack({
  title,
  summary,
  evidence,
  payload,
  trace,
  tone = "muted",
  emptyLabel = "insufficient evidence",
}: EvidenceStackProps) {
  const [level, setLevel] = useState<EvidenceLevel>("summary");

  return (
    <section className={cn("border bg-card/70 p-3", toneClass(tone))}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Evidence Stack</div>
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        </div>
        <div className="flex flex-wrap gap-1">
          {LEVELS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setLevel(item.id)}
              className={cn(
                "border px-2 py-1 text-[10px] uppercase tracking-wider",
                level === item.id
                  ? "border-cyan/60 bg-cyan/10 text-cyan"
                  : "border-border text-muted-foreground hover:text-foreground",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-3">
        {level === "summary" ? (
          <p className="text-sm leading-6 text-muted-foreground">{summary || emptyLabel}</p>
        ) : null}
        {level === "evidence" ? <EvidenceRows rows={evidence} emptyLabel={emptyLabel} /> : null}
        {level === "payload" ? (
          <pre className="max-h-72 overflow-auto border border-border bg-background/60 p-3 text-xs text-muted-foreground">
            {formatPayload(payload)}
          </pre>
        ) : null}
        {level === "trace" ? <EvidenceRows rows={trace} emptyLabel={emptyLabel} /> : null}
      </div>
    </section>
  );
}
