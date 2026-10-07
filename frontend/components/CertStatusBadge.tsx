"use client";
import { CertStatus } from "@/lib/api";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { useLanguage } from "@/components/LanguageProvider";
const tones: Record<CertStatus, BadgeTone> = {
  expired: "danger", expiring_soon: "warning", valid: "success", unknown: "neutral",
};
export function CertStatusBadge({ status }: { status: CertStatus }) {
  const { t } = useLanguage();
  return <Badge tone={tones[status]}>{t(`certStatus.${status}`)}</Badge>;
}
