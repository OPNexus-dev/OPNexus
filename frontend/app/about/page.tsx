"use client";

import { DataTable } from "@/components/ui/DataTable";
import { EmptyState } from "@/components/ui/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { Logo } from "@/components/Logo";
import { Card } from "@/components/ui/Card";
import { Spinner } from "@/components/ui/Spinner";
import { StatusDot } from "@/components/ui/StatusDot";
import { api, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { APP_CODENAME, APP_MAINTAINER, APP_VERSION } from "@/lib/version";
import { Network } from "lucide-react";
import { useEffect, useState } from "react";

interface HostDetails {
  firmware: { version: string | null; error: string | null; loading: boolean };
  ruleCount: { count: number | null; error: string | null; loading: boolean };
}

export default function AboutPage() {
  const { t, errorMessage } = useLanguage();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [details, setDetails] = useState<Record<string, HostDetails>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listHosts()
      .then((list) => {
        setHosts(list);
        setDetails(
          Object.fromEntries(
            list.map((h) => [
              h.id,
              {
                firmware: { version: null, error: null, loading: true },
                ruleCount: { count: null, error: null, loading: true },
              },
            ])
          )
        );

        for (const host of list) {
          api
            .getFirmwareInfo(host.id)
            .then((info) => {
              setDetails((prev) => ({
                ...prev,
                [host.id]: { ...prev[host.id], firmware: { version: info.product_version, error: null, loading: false } },
              }));
            })
            .catch((e) => {
              setDetails((prev) => ({
                ...prev,
                [host.id]: {
                  ...prev[host.id],
                  firmware: { version: null, error: errorMessage(e, "cluster.error"), loading: false },
                },
              }));
            });

          api
            .getLiveRules(host.id)
            .then((rules) => {
              setDetails((prev) => ({
                ...prev,
                [host.id]: { ...prev[host.id], ruleCount: { count: rules.length, error: null, loading: false } },
              }));
            })
            .catch((e) => {
              setDetails((prev) => ({
                ...prev,
                [host.id]: {
                  ...prev[host.id],
                  ruleCount: { count: null, error: errorMessage(e, "cluster.error"), loading: false },
                },
              }));
            });
        }
      })
      .catch((e) => setError(errorMessage(e, "certs.hostsLoadFailed")));
  }, [errorMessage]);

  return (
    <div>
      <div className="mb-8">
        <h1 className="sr-only">{t("settings.about")}</h1>
        <Logo className="h-10 w-auto" />
        <p className="mt-2 text-sm text-slate-500">
          {t("about.tagline")}
        </p>
        <p className="mt-1 text-sm text-slate-400">
          {t("about.version", { version: APP_VERSION, codename: APP_CODENAME, maintainer: APP_MAINTAINER })}
        </p>
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      <h2 className="mb-3 text-lg font-semibold text-slate-900">{t("about.fleetStatus")}</h2>
      {hosts === null && <Card className="p-5" aria-busy="true"><span className="inline-flex items-center gap-2 text-slate-400"><Spinner size={13} />{t("common.loading")}</span></Card>}
      {hosts && (
        <DataTable
          label={t("about.fleetStatus")}
          rows={hosts}
          rowKey={(host) => host.id}
          empty={<EmptyState icon={Network} title={t("drift.noHosts")} />}
          columns={[
            { key: "host", header: t("certs.colHost"), priority: "primary", isTitle: true, className: "text-slate-900", cell: (host) => host.name },
            { key: "status", header: t("hosts.status"), priority: "primary", cell: (host) => <StatusDot status={host.status} /> },
            { key: "address", header: t("about.colAddress"), priority: "high", className: "text-slate-500", cell: (host) => host.base_url },
            { key: "carp", header: "CARP", priority: "medium", cell: (host) => host.carp_status ? <Badge tone={host.carp_status === "MASTER" ? "success" : "warning"}>{host.carp_status}</Badge> : <span className="text-slate-300">—</span> },
            { key: "cluster", header: t("hosts.cluster"), priority: "medium", className: "text-slate-500", cell: (host) => host.cluster_name ?? <span className="text-slate-300">—</span> },
            {
              key: "version", header: t("about.colVersion"), priority: "high", className: "text-slate-600",
              cell: (host) => { const d = details[host.id]; return d?.firmware.loading ? <Spinner size={13} /> : d?.firmware.version ? d.firmware.version : <span className="text-red-500" title={d?.firmware.error ?? undefined}>{t("about.unavailable")}</span>; },
            },
            {
              key: "rules", header: t("about.colLiveRules"), priority: "high", tabular: true, className: "text-slate-600",
              cell: (host) => { const d = details[host.id]; return d?.ruleCount.loading ? <Spinner size={13} /> : d?.ruleCount.count !== null && d?.ruleCount.count !== undefined ? d.ruleCount.count : <span className="text-red-500" title={d?.ruleCount.error ?? undefined}>{t("about.unavailable")}</span>; },
            },
          ]}
        />
      )}
    </div>
  );
}
