"use client";

/**
 * Bordered widget panel with a cyan ALL-CAPS title bar.
 *
 * This is the canonical content container going forward — replaces
 * <Card>/<CardHeader>/<CardTitle> for the Bloomberg aesthetic.  It
 * renders as a 1-px bordered box with a dense header strip, an
 * optional icon, and a flex body that children fill.
 */

import type { LucideIcon } from "lucide-react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

export interface WidgetFrameProps {
  title: string;
  icon?: LucideIcon;
  actions?: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Optional small accent text rendered right after the title. */
  subtitle?: string;
  children: React.ReactNode;
}

export function WidgetFrame({
  title,
  icon: Icon,
  actions,
  subtitle,
  className,
  bodyClassName,
  children,
}: WidgetFrameProps) {
  return (
    <section className={cn("widget flex min-w-0 flex-col", className)}>
      <div className="widget-header">
        <span className="flex items-center gap-1 text-cyan">
          {Icon ? (
            <Icon className="h-3 w-3" strokeWidth={2.5} />
          ) : (
            <ChevronRight className="h-3 w-3" strokeWidth={2.5} />
          )}
          <span className="font-semibold">{title}</span>
          {subtitle && (
            <span className="text-muted-foreground">· {subtitle}</span>
          )}
        </span>
        {actions && (
          <span className="flex items-center gap-1 text-muted-foreground">
            {actions}
          </span>
        )}
      </div>
      <div className={cn("widget-body flex-1 overflow-auto", bodyClassName)}>
        {children}
      </div>
    </section>
  );
}
