"use client";

import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { api, ApiError, GatewayHealth, GatewayState, Host } from "@/lib/api";
import { useNotAdmin } from "@/lib/useReadOnly";
import { Bell, BellOff, Globe, KeyRound, RefreshCw } from "lucide-react";
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

/** "API rights missing: gateways, services" with a tooltip that names the OPNsense permission to grant. */
export function ApiRightsBadge({ reads }: { reads: string[] }) {
  const { t } = useLanguage();
  const names = reads.map((r) => t(`gateway.rights.read.${r}`)).join(", ");
  const how = reads.map((r) => `${t(`gateway.rights.read.${r}`)}: ${t(`gateway.rights.acl.${r}`)}`).join(" · ");
  return (
    <Tooltip content={`${t("gateway.rights.hint")} ${how}`} focusable>
      <Badge tone="warning"><KeyRound size={12} aria-hidden="true" />{t("gateway.rights.badge", { reads: names })}</Badge>
    </Tooltip>
  );
}

/** Live gateway state of one firewall (host detail page). */
export function HostGateways({ host }: { host: Host }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  // The alert switch is a notification setting: admins only (the server enforces it too).
  const notAdmin = useNotAdmin();
  const [gateways, setGateways] = useState<GatewayHealth[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Two independent sources (stored sample / live read), merged, so neither answer can wipe the other.
  const [storedMissing, setStoredMissing] = useState<string[]>([]);
  const [liveDenied, setLiveDenied] = useState(false);
  const missing = liveDenied && !storedMissing.includes("gateways") ? [...storedMissing, "gateways"] : storedMissing;
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    setLiveDenied(false);
    // Stored data (newest sample): which side readings the API key may not take.
    api.hostApiRights(host.id).then((r) => setStoredMissing(r.missing ?? [])).catch(() => setStoredMissing([]));
    api.hostGateways(host.id).then(setGateways).catch((e) => {
      setGateways([]);
      if (e instanceof ApiError && e.code === "API_RIGHTS_MISSING") setLiveDenied(true);
      else setError(errorMessage(e, "gateway.loadFailed"));
    });
  }, [host.id, errorMessage]);

  async function toggleAlert(g: GatewayHealth) {
    setBusy(g.name);
    try {
      const result = await api.setGatewayAlert(host.id, g.name, !g.alert_enabled);
      setGateways((rows) => (rows ?? []).map((row) => (row.name === g.name ? { ...row, alert_enabled: result.alert_enabled } : row)));
      toast.show(t(result.alert_enabled ? "gateway.alert.nowOn" : "gateway.alert.nowOff", { name: g.name }), "success");
    } catch (e) {
      toast.show(errorMessage(e, "gateway.alert.failed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <div className="mb-3 mt-8 flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-slate-900">{t("gateway.title")}</h2>
        <Button type="button" variant="ghost" onClick={load} aria-label={t("common.refresh")}><RefreshCw size={14} />{t("common.refresh")}</Button>
      </div>
      <Card className="mb-8 p-5">
        {missing.length > 0 && <div className="mb-3"><ApiRightsBadge reads={missing} /></div>}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {!error && gateways === null && <Skeleton className="h-12 w-full" />}
        {!error && gateways !== null && gateways.length === 0 && !missing.includes("gateways") && <EmptyState icon={Globe} title={t("gateway.none")} />}
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
                  {!g.alert_enabled && <Badge tone="neutral">{t("gateway.alert.off")}</Badge>}
                  <GatewayStateBadge state={g.state} />
                  <Tooltip content={notAdmin ? t("gateway.alert.adminOnly") : t(g.alert_enabled ? "gateway.alert.tooltipOn" : "gateway.alert.tooltipOff")} focusable={notAdmin}>
                    <Button
                      type="button" variant="ghost" className="px-2 py-1"
                      aria-pressed={g.alert_enabled}
                      aria-label={t("gateway.alert.label", { name: g.name })}
                      disabled={notAdmin || busy === g.name}
                      onClick={() => toggleAlert(g)}
                    >
                      {g.alert_enabled ? <Bell size={14} aria-hidden="true" /> : <BellOff size={14} aria-hidden="true" />}
                    </Button>
                  </Tooltip>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
