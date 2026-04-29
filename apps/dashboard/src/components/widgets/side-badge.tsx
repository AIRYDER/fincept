import { ArrowDownRight, ArrowUpRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { Side } from "@/lib/types";

export function SideBadge({ side }: { side: Side }) {
  const buy = side === "buy";
  const Icon = buy ? ArrowUpRight : ArrowDownRight;
  return (
    <Badge variant={buy ? "long" : "short"} className="gap-1">
      <Icon className="h-3 w-3" />
      {buy ? "BUY" : "SELL"}
    </Badge>
  );
}
