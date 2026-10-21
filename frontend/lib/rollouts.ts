// Shared display helpers for firmware rollouts (1.25): status tones and translated codes.
import type { BadgeTone } from "@/components/ui/Badge";
import type { TranslationParams } from "@/lib/i18n";
import type { MaintenanceWindow, RolloutCheckResult, RolloutHostStatus, RolloutIssue, RolloutRunStatus } from "@/lib/api";

type Translate = (key: string, params?: TranslationParams) => string;

export const RUN_TONE: Record<RolloutRunStatus, BadgeTone> = {
  waiting_window: "neutral", preflight: "info", wave_running: "info", soaking: "info", gate: "info",
  awaiting_approval: "accent", resuming: "info", paused: "warning", succeeded: "success", cancelled: "neutral",
};

export const HOST_TONE: Record<RolloutHostStatus, BadgeTone> = {
  queued: "neutral", running: "info", verifying: "info", succeeded: "success", skipped: "neutral",
  failed: "danger", blocked: "warning", cancelled: "neutral",
};

export const WAVE_TONE: Record<string, BadgeTone> = {
  pending: "neutral", awaiting_approval: "accent", preflight: "info", running: "info", soaking: "info", gate: "info",
  passed: "success", skipped: "neutral", cancelled: "neutral",
};

export const RESULT_TONE: Record<RolloutCheckResult["result"], BadgeTone> = { ok: "success", warn: "warning", fail: "danger", skip: "neutral" };

export const CHECKS = ["online", "version", "carp", "gateways", "services", "locks", "compliance"] as const;

function stringParams(params: Record<string, unknown> | null | undefined): TranslationParams {
  const out: TranslationParams = {};
  for (const [k, v] of Object.entries(params ?? {})) out[k] = v === null || v === undefined ? "" : typeof v === "number" ? v : String(v);
  return out;
}

/** A translated text for a {code, params} pair under `prefix`, with a readable fallback for unknown codes. */
export function codeText(t: Translate, prefix: string, issue: RolloutIssue | RolloutCheckResult | null | undefined): string {
  if (!issue) return "";
  const key = `${prefix}.${issue.code}`;
  const text = t(key, stringParams(issue.params));
  return text === key ? issue.code : text;
}

export function pauseText(t: Translate, reason: string | null): string {
  if (!reason) return "";
  const key = `rollouts.pauseReason.${reason}`;
  const text = t(key);
  return text === key ? reason : text;
}

function failedItem(t: Translate, item: string): string {
  const [host, check] = item.split(":");
  const key = `rollouts.gate.check.${check}`;
  const label = t(key);
  return check ? `${host}: ${label === key ? check : label}` : item;
}

/** Human-readable pause details: known keys translated, failed checks as "host: check". */
export function pauseDetailText(t: Translate, detail: Record<string, unknown> | null | undefined): string {
  if (!detail) return "";
  return Object.entries(detail)
    .filter(([k, v]) => !["on_resume"].includes(k) && v !== null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => {
      const key = `rollouts.detailKey.${k}`;
      const label = t(key) === key ? k : t(key);
      const value = k === "failed" && Array.isArray(v) ? v.map((x) => failedItem(t, String(x))).join(", ") : Array.isArray(v) ? v.join(", ") : String(v);
      return `${label}: ${value}`;
    })
    .join(" · ");
}

export function windowSummary(t: Translate, w: MaintenanceWindow): string {
  const base = { time: w.start_time, tz: w.timezone, duration: w.duration_minutes };
  if (w.recurrence === "weekly") {
    return t("mwindows.summary.weekly", { ...base, days: w.weekdays.map((d) => t(`mwindows.dayName.${d}`)).join(", ") });
  }
  if (w.month_mode === "day") return t("mwindows.summary.day", { ...base, day: w.month_day ?? "" });
  return t("mwindows.summary.nth", { ...base, nth: t(`mwindows.nth.${w.month_nth}`), day: t(`mwindows.dayLong.${w.month_weekday}`) });
}

export const isActiveRun = (status: RolloutRunStatus) => !["succeeded", "cancelled"].includes(status);
