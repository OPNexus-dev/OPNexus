"use client";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import PortForwardPanel from "@/components/PortForwardPanel";
import OneToOnePanel from "@/components/OneToOnePanel";
import OutboundNatPanel from "@/components/OutboundNatPanel";
import { LiveNatDrift } from "@/components/LiveNatDrift";
import { Tabs } from "@/components/ui/Tabs";
import { useLanguage } from "@/components/LanguageProvider";

import { Loading } from "@/components/ui/Loading";
const TABS = ["Port Forward", "1:1 NAT", "Outbound NAT", "Live-Drift"] as const;

// Lets a search hit for a specific NAT rule (which knows its exact type)
// land directly on the right tab instead of always defaulting to Port
// Forward — each panel already supports its own `?rule=<id>` filter, but
// only once its tab has actually been mounted (see `visited` below).
const TYPE_TO_TAB: Record<string, (typeof TABS)[number]> = {
  "port-forward": "Port Forward",
  "one-to-one": "1:1 NAT",
  "outbound-nat": "Outbound NAT",
};

export default function NatPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("nat.loading")}</Loading>}>
      <NatContent />
    </Suspense>
  );
}

function NatContent() {
  const { t } = useLanguage();
  const params = useSearchParams();
  const initialTab = TYPE_TO_TAB[params.get("type") ?? ""] ?? "Port Forward";
  const [tab, setTab] = useState<string>(initialTab);
  const [visited, setVisited] = useState([initialTab]);
  return (
    <div>
      <h1 className="mb-4 text-2xl font-semibold">NAT</h1>
      <Tabs
        className="mb-6"
        items={TABS.map((name) => ({ key: name, label: name === "Live-Drift" ? t("nat.liveDrift") : name }))}
        active={tab}
        onChange={(key) => {
          const next = key as (typeof TABS)[number];
          setTab(next);
          setVisited((v) => (v.includes(next) ? v : [...v, next]));
        }}
      />
      {visited.includes("Port Forward") && (
        <div role="tabpanel" hidden={tab !== "Port Forward"}>
          <PortForwardPanel />
        </div>
      )}
      {visited.includes("1:1 NAT") && (
        <div role="tabpanel" hidden={tab !== "1:1 NAT"}>
          <OneToOnePanel />
        </div>
      )}
      {visited.includes("Outbound NAT") && (
        <div role="tabpanel" hidden={tab !== "Outbound NAT"}>
          <OutboundNatPanel />
        </div>
      )}
      {visited.includes("Live-Drift") && (
        <div role="tabpanel" hidden={tab !== "Live-Drift"}>
          <LiveNatDrift />
        </div>
      )}
    </div>
  );
}
