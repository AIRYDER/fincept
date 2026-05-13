"use client";

/**
 * RowActions — the three-button trailing cell on each strategy row.
 *
 * We intentionally show the icons inline rather than behind a
 * kebab/more-menu.  The trade-off analysis:
 *
 *   - Inline icons cost ~100px of horizontal space per row but
 *     eliminate a two-click interaction for every common operation.
 *   - A kebab menu would hide the affordances; an operator would
 *     have to click once just to discover that Edit exists.
 *
 * Since we have plenty of horizontal room in the table and the
 * dashboard is a single-operator tool (not a public SaaS), inline
 * wins.
 */

import { History, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

import { DeleteStrategyDialog } from "@/components/strategies/delete-strategy-dialog";
import { EditStrategyDialog } from "@/components/strategies/edit-strategy-dialog";
import type { StrategyConfigRow } from "@/lib/types";
import { cn } from "@/lib/utils";

export function RowActions({
  config,
  onHistory,
}: {
  config: StrategyConfigRow;
  /** If provided, the history button is shown and clicks invoke this. */
  onHistory?: (strategyId: string) => void;
}) {
  const [editOpen, setEditOpen] = useState(false);

  return (
    <div className="flex items-center gap-0.5" onClick={(e) => e.stopPropagation()}>
      {onHistory ? (
        <IconButton
          title="View history"
          onClick={() => onHistory(config.strategy_id)}
          icon={History}
        />
      ) : null}
      <IconButton
        title="Edit"
        onClick={() => setEditOpen(true)}
        icon={Pencil}
      />
      <EditStrategyDialog
        config={config}
        open={editOpen}
        onOpenChange={setEditOpen}
      />
      <DeleteStrategyDialog
        config={config}
        trigger={<IconButton title="Delete" icon={Trash2} tone="destructive" />}
      />
    </div>
  );
}

function IconButton({
  icon: Icon,
  title,
  onClick,
  tone,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  onClick?: () => void;
  tone?: "destructive";
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      className={cn(
        "flex h-6 w-6 items-center justify-center border border-transparent text-muted-foreground transition-colors",
        tone === "destructive"
          ? "hover:border-destructive/60 hover:bg-destructive/5 hover:text-destructive"
          : "hover:border-border hover:bg-accent hover:text-foreground",
      )}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
}
