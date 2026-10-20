"use client";

import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { useLanguage } from "@/components/LanguageProvider";
import { api, GatewayHealth, GatewayState, Host } from "@/lib/api";
import { Globe, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

const TONE: Record<GatewayState, BadgeTone> = {
  online: "success", degraded: "warning", offline: "danger", forced_down: "neutral", unknown: "neutral",
};

export function GatewayStateBadge({ state }: { state: GatewayState }) {
  const { t } = useLanguage();
  return <Badge tone={TONE[state]}>{t(`gateway.state.${state}`)}</Badge>;
}

export function gatewayNumbers(g: GatewayHealth, t: (key: string, params?: Record<string, string | number>) => string): string {
  if (g.loss === null && g.delay === null) return t("gateway.notMeasured");
  const parts: string[] = [];
  if (g.loss !== null) parts.push(t("gateway.loss", { value: g.loss }));
  if (g.delay !== null) parts.push(t("gateway.delay", { value: g.delay }));
  if (g.stddev !== null) parts.push(t("gateway.stddev", { value: g.stddev }));
  return parts.join(" · ");
}

/** Live gateway state of one firewall (host detail page). */
export function HostGateways({ host }: { host: Host }) {
  const { t, errorMessage } = useLanguage();
  const [gateways, setGateways] = useState<GatewayHealth[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    api.hostGateways(host.id).then(setGateways).catch((e) => { setGateways([]); setError(errorMessage(e, "gateway.loadFailed")); });
  }, [host.id, errorMessage]);

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <div className="mb-3 mt-8 flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-slate-900">{t("gateway.title")}</h2>
        <Button type="button" variant="ghost" onClick={load} aria-label={t("common.refresh")}><RefreshCw size={14} />{t("common.refresh")}</Button>
      </div>
      <Card className="mb-8 p-5">
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {!error && gateways === null && <Skeleton className="h-12 w-full" />}
        {!error && gateways !== null && gateways.length === 0 && <EmptyState icon={Globe} title={t("gateway.none")} />}
        {gateways !== null && gateways.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {gateways.map((g) => (
              <li key={g.name} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <span className="min-w-0">
                  <span className="font-medium text-slate-900">{g.name}</span>
                  {g.address && <span className="text-slate-500"> · {g.address}</span>}
                </span>
                <span className="flex flex-wrap items-center gap-3">
                  <span className="text-xs text-slate-500">{gatewayNumbers(g, t)}</span>
                  <GatewayStateBadge state={g.state} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
