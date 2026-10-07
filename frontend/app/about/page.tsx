"use client";

import { Logo } from "@/components/Logo";
import { Card } from "@/components/ui/Card";
import { Spinner } from "@/components/ui/Spinner";
import { StatusDot } from "@/components/ui/StatusDot";
import { api, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { APP_CODENAME, APP_MAINTAINER, APP_VERSION } from "@/lib/version";
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
      <Card className="overflow-x-auto" tabIndex={0}>
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-5 py-3 font-medium">{t("certs.colHost")}</th>
              <th className="px-5 py-3 font-medium">{t("about.colAddress")}</th>
              <th className="px-5 py-3 font-medium">{t("hosts.status")}</th>
              <th className="px-5 py-3 font-medium">CARP</th>
              <th className="px-5 py-3 font-medium">{t("hosts.cluster")}</th>
              <th className="px-5 py-3 font-medium">{t("about.colVersion")}</th>
              <th className="px-5 py-3 font-medium">{t("about.colLiveRules")}</th>
            </tr>
          </thead>
          <tbody>
            {hosts === null &&
              [0, 1].map((i) => (
                <tr key={i} className="border-b border-slate-100 last:border-0">
                  <td colSpan={7} className="px-5 py-4">
                    <span className="inline-flex items-center gap-2 text-slate-400">
                      <Spinner size={13} />
                      {t("common.loading")}
                    </span>
                  </td>
                </tr>
              ))}
            {hosts?.length === 0 && (
              <tr>
                <td colSpan={7} className="px-5 py-8 text-center text-slate-400">
                  {t("drift.noHosts")}
                </td>
              </tr>
            )}
            {hosts?.map((host) => {
              const d = details[host.id];
              return (
                <tr key={host.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-5 py-3 font-medium text-slate-900">{host.name}</td>
                  <td className="px-5 py-3 text-slate-500">{host.base_url}</td>
                  <td className="px-5 py-3">
                    <StatusDot status={host.status} />
                  </td>
                  <td className="px-5 py-3">
                    {host.carp_status ? (
                      <span
                        className={
                          host.carp_status === "MASTER"
                            ? "rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700"
                            : "rounded-full bg-flame-100 px-2.5 py-0.5 text-xs font-medium text-flame-700"
                        }
                      >
                        {host.carp_status}
                      </span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-5 py-3 text-slate-500">{host.cluster_name ?? <span className="text-slate-300">—</span>}</td>
                  <td className="px-5 py-3 text-slate-600">
                    {d?.firmware.loading ? (
                      <Spinner size={13} />
                    ) : d?.firmware.version ? (
                      d.firmware.version
                    ) : (
                      <span className="text-red-500" title={d?.firmware.error ?? undefined}>
                        {t("about.unavailable")}
                      </span>
                    )}
                  </td>
                  <td className="px-5 py-3 text-slate-600">
                    {d?.ruleCount.loading ? (
                      <Spinner size={13} />
                    ) : d?.ruleCount.count !== null && d?.ruleCount.count !== undefined ? (
                      d.ruleCount.count
                    ) : (
                      <span className="text-red-500" title={d?.ruleCount.error ?? undefined}>
                        {t("about.unavailable")}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
