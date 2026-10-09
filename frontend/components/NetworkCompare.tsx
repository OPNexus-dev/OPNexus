"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { Table, TBody, TD, TH, THead, TRow } from "@/components/ui/Table";
import { api, ClusterNetworkDrift } from "@/lib/api";
import { CheckCircle2, Layers } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

const CATEGORY_TAB: Record<string, string> = {
  interfaces: "interfaces", vlans: "vlans", vips: "vips", gateways: "gateways", routes: "routes",
  dns_hosts: "dns", dhcp_hosts: "dhcp", kea_subnets: "dhcp", kea_reservations: "dhcp",
};

/** Network configuration differences between the nodes of every HA cluster. */
export function NetworkCompare() {
  const { t, errorMessage } = useLanguage();
  const [clusters, setClusters] = useState<ClusterNetworkDrift[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getNetworkDrift()
      .then((d) => setClusters(d.clusters))
      .catch((e) => setError(errorMessage(e, "network.compare.loadFailed")));
  }, [errorMessage]);

  if (error) return <p className="text-sm text-red-600">{error}</p>;
  if (!clusters) return <Skeleton className="h-48 w-full" />;
  if (clusters.length === 0) return <EmptyState icon={Layers} title={t("network.compare.noClusters")} />;

  return (
    <div className="space-y-6">
      <p className="text-sm text-slate-600">{t("network.compare.intro")}</p>
      {clusters.map((c) => (
        <Card key={c.name}>
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-5 py-3">
            <h2 className="text-sm font-semibold text-slate-700">
              {c.name} <span className="font-normal text-slate-500">({c.hosts.join(", ")})</span>
            </h2>
            {c.unreachable.length > 0 ? (
              <Badge tone="warning">{t("network.compare.unreachable", { hosts: c.unreachable.join(", ") })}</Badge>
            ) : c.differences.length === 0 ? (
              <Badge tone="success">
                <CheckCircle2 size={12} aria-hidden /> {t("network.compare.same")}
              </Badge>
            ) : (
              <Badge tone="danger">{t("network.compare.count", { count: c.differences.length })}</Badge>
            )}
          </div>
          {c.differences.length > 0 && (
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <TRow>
                    <TH>{t("network.compare.category")}</TH>
                    <TH>{t("network.compare.object")}</TH>
                    <TH>{t("network.compare.state")}</TH>
                    {c.compared.map((h) => (
                      <TH key={h}>{h}</TH>
                    ))}
                  </TRow>
                </THead>
                <TBody>
                  {c.differences.map((d) => (
                    <TRow key={`${d.category}:${d.key}`}>
                      <TD>
                        <Link className="text-brand-600 hover:underline" href={`/network/${CATEGORY_TAB[d.category] ?? "interfaces"}`}>
                          {t(`network.compare.categories.${d.category}`)}
                        </Link>
                      </TD>
                      <TD className="font-mono text-xs">{d.key}</TD>
                      <TD>
                        <Badge tone={d.status === "missing" ? "danger" : "warning"}>
                          {d.status === "missing" ? t("network.compare.missing") : t("network.compare.different")}
                        </Badge>
                      </TD>
                      {c.compared.map((h) => (
                        <TD key={h} className="font-mono text-xs">
                          {d.signatures[h] === undefined ? <span className="text-red-600">{t("network.compare.absent")}</span> : d.signatures[h] || "✓"}
                        </TD>
                      ))}
                    </TRow>
                  ))}
                </TBody>
              </Table>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
