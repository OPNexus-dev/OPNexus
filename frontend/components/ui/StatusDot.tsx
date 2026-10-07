"use client";

import clsx from "clsx";
import { useLanguage } from "@/components/LanguageProvider";
import { HostStatus } from "@/lib/api";

const colors: Record<HostStatus, string> = {
  online: "bg-emerald-500",
  offline: "bg-red-500",
  disabled: "bg-slate-300",
};

const labelKeys: Record<HostStatus, string> = {
  online: "common.statusOnline",
  offline: "common.statusOffline",
  disabled: "common.statusDisabled",
};

export function StatusDot({ status }: { status: HostStatus }) {
  const { t } = useLanguage();
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden="true" className={clsx("h-2.5 w-2.5 rounded-full", colors[status])} />
      <span className="text-sm text-slate-600">{t(labelKeys[status])}</span>
    </span>
  );
}
