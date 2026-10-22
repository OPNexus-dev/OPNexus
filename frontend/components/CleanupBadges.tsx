"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Tooltip } from "@/components/ui/Tooltip";
import type { AnalysisConfidence, CleanupKind } from "@/lib/api";
import clsx from "clsx";
import { Check, CircleHelp } from "lucide-react";

export const KIND_TONE: Record<CleanupKind, BadgeTone> = { unused: "warning", shadowed: "accent", duplicate: "accent", disabled_long: "neutral" };
const CONFIDENCE_TONE: Record<AnalysisConfidence, BadgeTone> = { sure: "info", probable: "warning", unknown: "neutral" };

export function ConfidenceBadge({ confidence }: { confidence: AnalysisConfidence }) {
  const { t } = useLanguage();
  return (
    <Tooltip content={t(`cleanup.confidence.tip.${confidence}`)} focusable>
      <Badge
        tone={CONFIDENCE_TONE[confidence]}
        className={clsx(confidence === "unknown" && "border-dashed")}
        aria-label={`${t("cleanup.confidence.label")}: ${t(`cleanup.confidence.${confidence}`)}`}
      >
        {confidence === "unknown" ? <CircleHelp size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />}
        {t(`cleanup.confidence.${confidence}`)}
      </Badge>
    </Tooltip>
  );
}

/** A note code of the analysis or the cleanup plan (cleanup.notes.*); unknown codes show raw. */
export function NoteBadge({ code }: { code: string }) {
  const { t } = useLanguage();
  const label = t(`cleanup.notes.${code}`);
  const known = label !== `cleanup.notes.${code}`;
  return (
    <Tooltip content={known ? t(`cleanup.notes.${code}_hint`) : code} focusable>
      <Badge tone="warning">{known ? label : code}</Badge>
    </Tooltip>
  );
}
