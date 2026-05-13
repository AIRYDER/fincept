"use client";

import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { AllocationBucket, PortfolioHolding } from "./portfolioBuilder.types";

const COLORS = ["#ff8a00", "#00e5ff", "#19c37d", "#f5c542", "#ff4d4f", "#8b5cf6", "#94a3b8"];

export function PortfolioCharts({
  sectorAllocations,
  assetTypeAllocations,
  riskBuckets,
  holdings,
}: {
  sectorAllocations: AllocationBucket[];
  assetTypeAllocations: AllocationBucket[];
  riskBuckets: AllocationBucket[];
  holdings: PortfolioHolding[];
}) {
  const topHoldings = [...holdings]
    .sort((a, b) => b.dollarAllocation - a.dollarAllocation)
    .slice(0, 10)
    .map((h) => ({ label: h.ticker, percent: h.percentAllocation }));

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <ChartPanel title="Sector allocation">
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={sectorAllocations.slice(0, 10)}>
            <CartesianGrid stroke="#222" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: "#888", fontSize: 10 }} interval={0} angle={-25} textAnchor="end" height={70} />
            <YAxis tick={{ fill: "#888", fontSize: 10 }} />
            <Tooltip contentStyle={{ background: "#050505", border: "1px solid #242424" }} />
            <Bar dataKey="percent" fill="#00e5ff" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartPanel>
      <ChartPanel title="Position weights">
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={topHoldings} layout="vertical">
            <CartesianGrid stroke="#222" horizontal={false} />
            <XAxis type="number" tick={{ fill: "#888", fontSize: 10 }} />
            <YAxis type="category" dataKey="label" tick={{ fill: "#ddd", fontSize: 11 }} width={52} />
            <Tooltip contentStyle={{ background: "#050505", border: "1px solid #242424" }} />
            <Bar dataKey="percent" fill="#ff8a00" radius={[0, 2, 2, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartPanel>
      <ChartPanel title="Asset type split">
        <PieBlock data={assetTypeAllocations} />
      </ChartPanel>
      <ChartPanel title="Risk buckets">
        <PieBlock data={riskBuckets} />
      </ChartPanel>
    </div>
  );
}

function PieBlock({ data }: { data: AllocationBucket[] }) {
  return (
    <ResponsiveContainer width="100%" height={250}>
      <PieChart>
        <Pie data={data} dataKey="percent" nameKey="label" innerRadius={60} outerRadius={95} paddingAngle={2}>
          {data.map((entry, index) => (
            <Cell key={entry.label} fill={COLORS[index % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip contentStyle={{ background: "#050505", border: "1px solid #242424" }} />
      </PieChart>
    </ResponsiveContainer>
  );
}

function ChartPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border border-border">
      <div className="border-b border-border px-3 py-2 text-[10px] uppercase tracking-wider text-cyan">
        {title}
      </div>
      <div className="p-3">{children}</div>
    </section>
  );
}
