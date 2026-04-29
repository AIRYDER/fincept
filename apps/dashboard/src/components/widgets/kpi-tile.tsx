"use client";

import { motion } from "framer-motion";
import { LucideIcon, Minus, TrendingDown, TrendingUp } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { cn, pnlClass } from "@/lib/utils";

export interface KpiTileProps {
  label: string;
  value: string;
  icon?: LucideIcon;
  sub?: string;
  /** Optional numeric delta - drives the colored arrow + tint. */
  delta?: number | null;
  /** Optional sparkline children (e.g. <Sparkline ...>). */
  children?: React.ReactNode;
}

export function KpiTile({
  label,
  value,
  icon: Icon,
  sub,
  delta,
  children,
}: KpiTileProps) {
  const direction =
    delta === null || delta === undefined || delta === 0
      ? "flat"
      : delta > 0
        ? "up"
        : "down";
  const Arrow =
    direction === "up" ? TrendingUp : direction === "down" ? TrendingDown : Minus;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
    >
      <Card className="relative overflow-hidden">
        <CardContent className="flex flex-col gap-2 p-5">
          <div className="flex items-center gap-2">
            {Icon ? (
              <div className="rounded-md bg-primary/10 p-1.5 text-primary">
                <Icon className="h-3.5 w-3.5" />
              </div>
            ) : null}
            <span className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">
              {label}
            </span>
          </div>
          <div className="flex items-baseline gap-2">
            <span className="num text-3xl font-semibold tracking-tight">
              {value}
            </span>
            {delta !== undefined && delta !== null ? (
              <span
                className={cn(
                  "inline-flex items-center gap-0.5 text-xs font-medium",
                  pnlClass(delta),
                )}
              >
                <Arrow className="h-3 w-3" />
                {Math.abs(delta).toLocaleString("en-US", {
                  maximumFractionDigits: 2,
                })}
              </span>
            ) : null}
          </div>
          {sub ? (
            <span className="text-xs text-muted-foreground">{sub}</span>
          ) : null}
          {children ? <div className="-mx-5 -mb-5">{children}</div> : null}
        </CardContent>
      </Card>
    </motion.div>
  );
}
