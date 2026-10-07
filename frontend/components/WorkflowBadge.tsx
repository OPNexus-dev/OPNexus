"use client";
import { Rule } from "@/lib/api";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { useLanguage } from "@/components/LanguageProvider";

export const workflowStates: Rule["workflow_state"][] = ["attention", "pending", "cleanup", "unassigned", "disabled", "draft", "synced"];
const tones: Record<Rule["workflow_state"], BadgeTone> = {
  attention: "danger", pending: "warning", cleanup: "warning",
  unassigned: "neutral", disabled: "neutral", draft: "info", synced: "success",
};
export function WorkflowBadge({ state }: { state: Rule["workflow_state"] }) {
  const { t } = useLanguage();
  return (
    <Badge tone={tones[state]} title={t("workflow.tooltip")}>
      {t(`workflow.${state}`)}
    </Badge>
  );
}
