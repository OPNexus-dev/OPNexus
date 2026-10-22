import type { AnalysisConfidence, CleanupCandidate, CleanupItem, CleanupOperation, CleanupOperationStatus } from "@/lib/api";

/** Version of the fixed acknowledgement text (cleanup.ack.text); the backend stores it with the audit entry. */
export const ACK_TEXT_VERSION = "1";
/** Same threshold as the backend: below a 30-day window "unused" is at most "probable" (monthly and quarterly jobs). */
export const MIN_SURE_UNUSED_DAYS = 30;
/** An operation in one of these states blocks everything else on the host/cluster until it is resolved. */
export const OPEN_STATUSES: CleanupOperationStatus[] = ["running", "awaiting_confirm", "partial", "apply_failed", "reverting", "revert_failed"];
/** States with an armed automatic revert (countdown). */
export const COUNTDOWN_STATUSES: CleanupOperationStatus[] = ["awaiting_confirm", "partial", "apply_failed"];
/** Hidden from the history: never executed, nothing happened. */
const NOISE_STATUSES: CleanupOperationStatus[] = ["preview", "cancelled", "expired"];

export type BlockReason = "automatic" | "legacy" | "managed" | "confidence" | "nothing" | "deleteWait" | "unreadable";
export type Selectability =
  | { mode: "disable"; level: AnalysisConfidence; needsAck: boolean }
  | { mode: "delete"; level: AnalysisConfidence; needsAck: false }
  | { mode: null; reason: BlockReason };

const RANK: Record<AnalysisConfidence, number> = { unknown: 0, probable: 1, sure: 2 };

/**
 * What the user may do with a candidate row. A client-side mirror of the backend rules (selectable_level) that only
 * decides how the row looks: the backend recomputes everything at preview and may still refuse.
 * - "unused" is at most "probable" for a block/reject rule and for a window shorter than 30 days,
 * - "unknown" is never selectable, "probable" needs the rule's own acknowledgement,
 * - an already disabled rule can only be deleted ("sure" disabled_long); everything else goes through "disable".
 */
export function selectability(candidate: CleanupCandidate, unusedDays: number): Selectability {
  if (!candidate.removable) return { mode: null, reason: candidate.not_removable_reason ?? "automatic" };
  if (!candidate.uuid) return { mode: null, reason: "unreadable" };
  if (!candidate.enabled) {
    const long = candidate.categories.find((c) => c.kind === "disabled_long");
    if (long?.confidence === "sure") return { mode: "delete", level: "sure", needsAck: false };
    return { mode: null, reason: long ? "deleteWait" : "nothing" };
  }
  let best: AnalysisConfidence | null = null;
  for (const category of candidate.categories) {
    if (category.kind === "disabled_long") continue;
    let level = category.confidence;
    if (category.kind === "unused" && (candidate.action === "block" || candidate.action === "reject" || unusedDays < MIN_SURE_UNUSED_DAYS)) {
      if (RANK[level] > RANK.probable) level = "probable";
    }
    if (best === null || RANK[level] > RANK[best]) best = level;
  }
  if (best === null) return { mode: null, reason: "nothing" };
  if (best === "unknown") return { mode: null, reason: "confidence" };
  return { mode: "disable", level: best, needsAck: best === "probable" };
}

/** An ErrorCode name ("BEREINIGUNG_...", optionally followed by ": detail") is translated; the firewall's own text stays raw. */
export function cleanupMessage(t: (key: string) => string, message: string | null | undefined): string {
  if (!message) return "";
  const match = /^([A-Z][A-Z0-9_]{3,})(?::\s*([\s\S]*))?$/.exec(message);
  if (match) {
    const key = `errors.${match[1]}`;
    const text = t(key);
    if (text !== key) return match[2] ? `${text} ${match[2]}` : text;
  }
  return message;
}

/** The operation that blocks the host right now, if any (the newest one). */
export function isOpen(op: CleanupOperation): boolean {
  // A delete/restore that ended with an unclear item is still unresolved (and blocks the host) until it is read again.
  return OPEN_STATUSES.includes(op.status) || (op.can_retry && !NOISE_STATUSES.includes(op.status));
}

export function activeOperation(operations: CleanupOperation[]): CleanupOperation | null {
  return operations.find(isOpen) ?? null;
}

export function historyOperations(operations: CleanupOperation[]): CleanupOperation[] {
  return operations.filter((op) => !NOISE_STATUSES.includes(op.status) && !isOpen(op));
}

/** Rule identities (the UUID it has now and the one it had) for which a newer operation exists, per node. */
export function latestItemIds(operations: CleanupOperation[], hostId: string): Set<string> {
  const seen = new Set<string>();
  const latest = new Set<string>();
  for (const op of operations) {
    if (NOISE_STATUSES.includes(op.status)) continue;
    for (const item of op.items) {
      if (item.host_id !== hostId) continue;
      const keys = [item.rule_uuid, item.restored_uuid].filter((k): k is string => !!k).map((k) => k.toLowerCase());
      if (keys.some((k) => seen.has(k))) continue;
      latest.add(item.id);
    }
    for (const item of op.items) {
      if (item.host_id !== hostId) continue;
      for (const key of [item.rule_uuid, item.restored_uuid]) if (key) seen.add(key.toLowerCase());
    }
  }
  return latest;
}

/** The UUID under which a rule can be switched back on or deleted now (a restored rule has a new one). */
export function currentUuid(item: CleanupItem, operationKind: string): string {
  return operationKind === "restore" && item.restored_uuid ? item.restored_uuid : item.rule_uuid;
}

export function secondsUntil(deadline: string | null | undefined, now: number): number {
  if (!deadline) return 0;
  return Math.max(0, Math.ceil((new Date(deadline).getTime() - now) / 1000));
}

export function formatClock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Client-side check of the management sources, as strict as the backend (IP or CIDR, no names, not "everything"). */
/** Extra management ports: TCP ports 1-65535, separated by spaces/commas, at most `max`; sorted, no duplicates. */
export function parsePorts(text: string, max = 20): { ports: number[]; error: "invalid" | "tooMany" | null; bad?: string } {
  const ports: number[] = [];
  for (const raw of text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean)) {
    if (!/^\d{1,5}$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) return { ports, error: "invalid", bad: raw };
    if (!ports.includes(Number(raw))) ports.push(Number(raw));
  }
  ports.sort((a, b) => a - b);
  if (ports.length > max) return { ports, error: "tooMany" };
  return { ports, error: null };
}

export function parseSources(text: string, max = 20): { entries: string[]; error: "empty" | "invalid" | "everything" | "tooMany" | null; bad?: string } {
  const entries: string[] = [];
  for (const raw of text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean)) {
    const [address, prefix, ...rest] = raw.split("/");
    if (rest.length > 0) return { entries, error: "invalid", bad: raw };
    const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(address);
    let max_prefix = 0;
    if (v4) {
      if (v4.slice(1).some((o) => Number(o) > 255 || (o.length > 1 && o.startsWith("0")))) return { entries, error: "invalid", bad: raw };
      max_prefix = 32;
    } else if (address.includes(":") && /^[0-9a-fA-F:.]+$/.test(address)) {
      try { new URL(`http://[${address}]/`); } catch { return { entries, error: "invalid", bad: raw }; }
      max_prefix = 128;
    } else {
      return { entries, error: "invalid", bad: raw };
    }
    if (prefix !== undefined) {
      if (!/^\d{1,3}$/.test(prefix) || Number(prefix) > max_prefix) return { entries, error: "invalid", bad: raw };
      if (Number(prefix) === 0) return { entries, error: "everything", bad: raw };
    }
    if (!entries.includes(raw)) entries.push(raw);
  }
  if (entries.length === 0) return { entries, error: "empty" };
  if (entries.length > max) return { entries, error: "tooMany" };
  return { entries, error: null };
}
