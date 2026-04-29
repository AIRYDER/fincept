"use client";

import { Area, AreaChart, ResponsiveContainer, Tooltip } from "recharts";

interface SparklinePoint {
  x: number;
  y: number;
}

export function Sparkline({
  data,
  positive = true,
  height = 56,
}: {
  data: SparklinePoint[];
  positive?: boolean;
  height?: number;
}) {
  const stroke = positive ? "hsl(var(--long))" : "hsl(var(--short))";
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={`spark-${positive ? "p" : "n"}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.4} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Tooltip
            cursor={false}
            contentStyle={{ display: "none" }}
            wrapperStyle={{ display: "none" }}
          />
          <Area
            type="monotone"
            dataKey="y"
            stroke={stroke}
            strokeWidth={1.5}
            fill={`url(#spark-${positive ? "p" : "n"})`}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
