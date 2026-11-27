import type { BadgeTone } from "@/components/ui/Badge";

/** Colour of an audit result/step state. The text always comes with the colour, so this never carries meaning alone. */
export function auditTone(status: string): BadgeTone {
  switch (status) {
    case "success":
      return "success";
    case "error":
    case "revert_failed":
    case "denied":
    case "rejected":
      return "danger";
    case "pending":
    case "starting":
    case "submitted":
    case "interrupted":
    case "unknown":
      return "warning";
    case "reverted":
    case "removed":
    case "acknowledged":
      return "info";
    default:
      return "neutral"; // requested, accepted
  }
}

/** Start of a "last 24 hours / 7 days / 30 days" range as an ISO string; "" for no limit. */
export function sinceFor(range: string, now: Date = new Date()): string {
  const days = range === "24h" ? 1 : range === "7d" ? 7 : range === "30d" ? 30 : 0;
  return days ? new Date(now.getTime() - days * 86_400_000).toISOString() : "";
}
