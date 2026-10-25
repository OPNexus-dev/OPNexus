"use client";

import { Badge, BadgeTone, badgeToneClasses } from "@/components/ui/Badge";
import { InfoTip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, HaStatus } from "@/lib/api";
import { AlertTriangle } from "lucide-react";
import clsx from "clsx";
import { useEffect, useState } from "react";

const REFRESH_MS = 15000;

function mb(bytes: number | null | undefined, locale: string): string {
  return bytes == null ? "—" : (bytes / 1048576).toLocaleString(locale, { maximumFractionDigits: 1 });
}

function ageText(seconds: number): string {
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
  if (seconds >= 120) return `${Math.floor(seconds / 60)} min`;
  return `${seconds} s`;
}

/** Zustand des HA-Paars (nur lesend). Erscheint nur, wenn dieser Server Teil eines Paars ist; ein Wechsel geschieht
 *  absichtlich nicht per Klick (der alte Primär muss sicher angehalten werden), sondern mit `opnexus ha promote`. */
export function HaSection() {
  const { t, locale } = useLanguage();
  const [status, setStatus] = useState<HaStatus | null>(null);

  useEffect(() => {
    let active = true;
    const load = () => api.haStatus().then((next) => active && setStatus(next)).catch(() => active && setStatus(null));
    void load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  if (!status || !status.configured) return null;

  const role = status.split_brain ? "fenced" : (status.db_role ?? "unknown");
  const roleTone: BadgeTone = status.split_brain ? "danger" : role === "primary" ? "success" : role === "replica" ? "info" : "neutral";
  const rep = status.replication;
  const slot = status.slot;
  const partnerFenced = status.peer?.reachable && status.peer.role === "fenced";
  const lagSeconds = rep?.lag_seconds != null ? rep.lag_seconds.toLocaleString(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—";

  const rows: [string, React.ReactNode][] = [
    [t("settings.ha.node"), <span key="n" className="flex items-center gap-2">{status.node}<Badge tone={roleTone}>{t(`settings.ha.role.${role}`)}</Badge></span>],
    [t("settings.ha.epoch"), <span key="e" className="flex items-center gap-1">{status.epoch}<InfoTip text={t("settings.ha.epochHint")} /></span>],
    [t("settings.ha.primaryNode"), status.primary_node ?? "—"],
    [t("settings.ha.promotedAt"), status.promoted_at ? new Date(status.promoted_at).toLocaleString(locale) : t("settings.ha.never")],
    [t("settings.ha.vip"), status.vip ?? "—"],
    [
      t("settings.ha.partner"),
      status.peer?.reachable
        ? `${status.peer_ip}: ${t("settings.ha.partnerReachable", { role: String(status.peer.role), epoch: String(status.peer.epoch) })}`
        : `${status.peer_ip}: ${t("settings.ha.partnerSilent")}`,
    ],
    [
      t("settings.ha.replication"),
      rep?.state ? t("settings.ha.replicationLag", { state: rep.state, mb: mb(rep.lag_bytes, locale), s: lagSeconds }) : status.db_role === "primary" ? t("settings.ha.replicationNone") : "—",
    ],
    ...(status.db_role === "primary"
      ? ([[
          t("settings.ha.slot"),
          slot?.present
            ? t("settings.ha.slotValue", {
                status: String(slot.wal_status ?? "—"), mb: mb(slot.retained_bytes, locale),
                limit: slot.limit_bytes != null ? `${mb(slot.limit_bytes, locale)} MB` : t("settings.ha.slotNoLimit"),
              })
            : t("settings.ha.slotMissing"),
        ]] as [string, React.ReactNode][])
      : []),
    [t("settings.ha.sync"), status.sync_age_seconds != null ? t("settings.ha.syncAge", { age: ageText(status.sync_age_seconds) }) : t("settings.ha.syncNever")],
  ];

  return (
    <section id="ha" className="scroll-mt-4 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("settings.ha.title")}
        <InfoTip text={t("settings.ha.hint")} />
      </h2>
      {status.split_brain && (
        <p role="alert" className={clsx("flex items-start gap-2 rounded-lg border p-3 text-sm", badgeToneClasses.danger)}>
          <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t("settings.ha.splitBrain")}
        </p>
      )}
      {partnerFenced && (
        <p role="alert" className={clsx("flex items-start gap-2 rounded-lg border p-3 text-sm", badgeToneClasses.warning)}>
          <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t("settings.ha.partnerFenced")}
        </p>
      )}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[minmax(0,14rem)_1fr]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-slate-600">{label}</dt>
            <dd className="min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-slate-500">{t("settings.ha.commands")}</p>
    </section>
  );
}
