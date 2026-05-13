"use client";

/**
 * useApiErrorMessage — extract a user-facing message from a React-
 * Query error.  All four strategy mutations (create, update, delete,
 * lifecycle) share the same error shape so centralising here keeps
 * the dialog code readable and the tone mapping consistent.
 *
 * The tone classification is deliberate: 409 (conflict) and 429
 * (rate-limit) are *recoverable* and styled as warnings; 400 / 404 /
 * 500 are genuine problems and styled destructive.
 */

import { ApiError } from "@/lib/api";

export type ApiMessageTone = "warn" | "danger";

export interface ApiErrorMessage {
  tone: ApiMessageTone;
  text: string;
  status: number | null;
}

export function apiErrorMessage(err: unknown): ApiErrorMessage | null {
  if (!err) return null;
  if (err instanceof ApiError) {
    const detail =
      typeof err.body === "object" && err.body !== null && "detail" in err.body
        ? formatDetail((err.body as { detail: unknown }).detail)
        : err.message;
    const tone: ApiMessageTone =
      err.status === 409 || err.status === 429 ? "warn" : "danger";
    return { tone, text: detail, status: err.status };
  }
  return {
    tone: "danger",
    text: err instanceof Error ? err.message : String(err),
    status: null,
  };
}

/** Pydantic 422 bodies carry a list of per-field errors; flatten them. */
function formatDetail(detail: unknown): string {
  if (Array.isArray(detail)) {
    return detail
      .map((e) => {
        if (typeof e === "object" && e !== null) {
          const obj = e as Record<string, unknown>;
          const loc = Array.isArray(obj.loc)
            ? (obj.loc as unknown[]).slice(1).join(".")
            : null;
          const msg = String(obj.msg ?? "invalid");
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(e);
      })
      .join("; ");
  }
  return String(detail ?? "unknown error");
}
