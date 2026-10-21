"use client";

import { Badge } from "@/components/ui/Badge";
import { useLanguage } from "@/components/LanguageProvider";
import { api, RolloutStatus } from "@/lib/api";
import { pauseText, RUN_TONE } from "@/lib/rollouts";
import { Waves } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

/** "Rollout X – wave 2/3" strip for /updates and /rollouts. Polls while a run is active. */
export function RolloutStatusBanner({ onStatus }: { onStatus?: (status: RolloutStatus) => void }) {
  const { t } = useLanguage();
  const [status, setStatus] = useState<RolloutStatus | null>(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const next = await api.rolloutStatus();
        if (!alive) return;
        setStatus(next);
        onStatus?.(next);
      } catch {
        /* the banner is optional */
      }
    }
    load();
    const timer = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = status?.active;
  if (!run) return null;
  return (
    <div role="status" className="mb-4 flex flex-wrap items-center gap-2 rounded-xl border border-sky-200 bg-brand-50 px-4 py-3 text-sm text-slate-800">
      <Waves size={16} className="shrink-0 text-brand-600" aria-hidden />
      <span className="min-w-0 flex-1">
        {t("rollouts.banner.active", {
          plan: run.plan_name, wave: `${run.current_wave + 1}/${run.wave_count}`, label: run.current_wave_label ?? "",
          status: t(`rollouts.run.status.${run.status}`),
        })}
        {run.status === "paused" && run.pause_reason && <span className="text-slate-600"> — {pauseText(t, run.pause_reason)}</span>}
      </span>
      <Badge tone={RUN_TONE[run.status]}>{t(`rollouts.run.status.${run.status}`)}</Badge>
      <Link href={`/rollouts/runs/${run.id}`} className="font-medium text-brand-600 underline-offset-2 hover:underline">
        {t("rollouts.banner.open")}
      </Link>
    </div>
  );
}
