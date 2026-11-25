"use client";

import { DeploymentResult, RuleAction } from "@/lib/api";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle, CheckCircle2, Clock, RotateCcw, XCircle } from "lucide-react";

/** Pass / block / reject as a coloured badge (green, red, amber) - shared by the card list and the table. */
export function ActionBadge({ action }: { action: RuleAction }) {
  const { t } = useLanguage();
  const tones: Record<RuleAction, BadgeTone> = { pass: "success", block: "danger", reject: "warning" };
  const labels: Record<RuleAction, string> = { pass: t("rules.actionPass"), block: t("rules.actionBlock"), reject: t("rules.actionReject") };
  return <Badge tone={tones[action]}>{labels[action]}</Badge>;
}

export function DeploymentIcon({ status }: { status: DeploymentResult["status"] }) {
  switch (status) {
    case "success":
      return <CheckCircle2 size={15} className="text-emerald-500" />;
    case "error":
    case "revert_failed":
      return <AlertTriangle size={15} className="text-red-500" />;
    case "pending":
      return <Clock size={15} className="text-flame-500" />;
    case "reverted":
      return <RotateCcw size={15} className="text-slate-400" />;
    default:
      return <XCircle size={15} className="text-red-500" />;
  }
}
