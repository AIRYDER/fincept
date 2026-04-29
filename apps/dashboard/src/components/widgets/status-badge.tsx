import { Badge, type BadgeProps } from "@/components/ui/badge";
import type { OrderStatus } from "@/lib/types";

const STATUS_VARIANT: Record<OrderStatus, NonNullable<BadgeProps["variant"]>> = {
  pending_new: "warn",
  new: "default",
  partially_filled: "warn",
  filled: "long",
  cancelled: "muted",
  rejected: "destructive",
  expired: "muted",
};

const STATUS_LABEL: Record<OrderStatus, string> = {
  pending_new: "PENDING",
  new: "NEW",
  partially_filled: "PARTIAL",
  filled: "FILLED",
  cancelled: "CANCELLED",
  rejected: "REJECTED",
  expired: "EXPIRED",
};

export function OrderStatusBadge({ status }: { status: OrderStatus }) {
  return (
    <Badge variant={STATUS_VARIANT[status]}>{STATUS_LABEL[status]}</Badge>
  );
}
