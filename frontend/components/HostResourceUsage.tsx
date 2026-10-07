"use client";

import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { Sparkline, SparklinePoint } from "@/components/ui/Sparkline";
import { api, Host, HostResourceSample } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

function toPoints(samples: HostResourceSample[], pick: (s: HostResourceSample) => number | null): SparklinePoint[] {
  return samples
    .filter((s) => pick(s) !== null)
    .map((s) => ({ t: s.sampled_at, v: pick(s) as number }));
}

export function HostResourceUsage({ host }: { host: Host }) {
  const { t, errorMessage } = useLanguage();
  const [samples, setSamples] = useState<HostResourceSample[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listResourceSamples(host.id, 24)
      .then(setSamples)
      .catch((e) => setError(errorMessage(e, "hostResource.loadFailed")));
  }, [host.id, errorMessage]);

  return (
    <>
      <h2 className="mb-3 mt-8 text-lg font-semibold text-slate-900">{t("hostResource.title")}</h2>
      <Card className="mb-8 p-5">
        {error && <p className="text-sm text-red-600">{error}</p>}
        {!error && !samples && (
          <div className="grid gap-6 sm:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        )}
        {samples && samples.length === 0 && (
          <p className="py-6 text-center text-slate-400">
            {t("hostResource.empty")}
          </p>
        )}
        {samples && samples.length > 0 && (
          <div className="grid gap-6 sm:grid-cols-3">
            <Sparkline
              label={t("hostResource.cpu")}
              points={toPoints(samples, (s) => s.load_avg_1m)}
              format={(v) => v.toFixed(2)}
            />
            <Sparkline
              label={t("hostResource.memory")}
              points={toPoints(samples, (s) => s.mem_used_pct)}
              format={(v) => `${v.toFixed(0)} %`}
            />
            <Sparkline
              label={t("hostResource.disk")}
              points={toPoints(samples, (s) => s.disk_used_pct)}
              format={(v) => `${v.toFixed(0)} %`}
            />
          </div>
        )}
      </Card>
    </>
  );
}
