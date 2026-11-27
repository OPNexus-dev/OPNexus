"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { DataTable } from "@/components/ui/DataTable";
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
            // A comparison only makes sense side by side: on a phone the table scrolls (with an edge fade and a fixed first column).
            <DataTable
              bare
              mobile="scroll"
              label={c.name}
              rows={c.differences}
              rowKey={(d) => `${d.category}:${d.key}`}
              columns={[
                {
                  key: "category", header: t("network.compare.category"), priority: "primary", isTitle: true,
                  cell: (d) => (
                    <Link className="text-brand-600 hover:underline" href={`/network/${CATEGORY_TAB[d.category] ?? "interfaces"}`}>
                      {t(`network.compare.categories.${d.category}`)}
                    </Link>
                  ),
                },
                { key: "object", header: t("network.compare.object"), priority: "primary", mono: true, cell: (d) => d.key },
                {
                  key: "state", header: t("network.compare.state"), priority: "primary",
                  cell: (d) => <Badge tone={d.status === "missing" ? "danger" : "warning"}>{d.status === "missing" ? t("network.compare.missing") : t("network.compare.different")}</Badge>,
                },
                ...c.compared.map((h) => ({
                  key: `host:${h}`, header: h, priority: "primary" as const, mono: true,
                  cell: (d: (typeof c.differences)[number]) => (d.signatures[h] === undefined ? <span className="text-red-600">{t("network.compare.absent")}</span> : d.signatures[h] || "✓"),
                })),
              ]}
            />
          )}
        </Card>
      ))}
    </div>
  );
}
