"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Select } from "@/components/ui/Field";
import { useToast } from "@/components/ui/Toast";
import { useLanguage } from "@/components/LanguageProvider";
import { AdoptAllResult, AdoptKind, api, Host, UnmanagedObject } from "@/lib/api";
import { useEffect, useMemo, useState } from "react";

interface Props {
  kind: AdoptKind;
  hosts: Host[];
  onClose: () => void;
  onAdopted: () => void;
}

const TITLE_KEY: Record<AdoptKind, string> = {
  aliases: "adopt.title.aliases",
  "port-forward": "adopt.title.portForward",
  "one-to-one": "adopt.title.oneToOne",
  "outbound-nat": "adopt.title.outboundNat",
};

// The alias types OPNexus manages (the others only show OPNsense's own English label).
const ALIAS_TYPES = ["host", "network", "port", "networkgroup", "mac", "asn"];

function endpoint(net: string | undefined, port: string | undefined): string {
  const base = net || "any";
  return port ? `${base}:${port}` : base;
}

function isSystem(item: UnmanagedObject): boolean {
  return item.reasons.length === 1 && item.reasons[0] === "system";
}

/**
 * Lists the aliases / NAT rules that exist on a firewall but are not managed by OPNexus yet and lets the
 * operator take them over, one by one or all adoptable ones at once. Taking over only records the object —
 * nothing is written to the firewall until it is changed and rolled out the normal way (same idea as
 * AdoptRulesModal for filter rules).
 */
export function AdoptObjectsModal({ kind, hosts, onClose, onAdopted }: Props) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const enabledHosts = hosts.filter((h) => h.enabled);
  // Derived rather than initialised once: the host list may still be loading when the dialog opens.
  const [chosenHostId, setHostId] = useState("");
  const hostId = chosenHostId || enabledHosts[0]?.id || "";
  const [items, setItems] = useState<UnmanagedObject[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<AdoptAllResult | null>(null);

  function load(id: string) {
    setItems(null);
    setError(null);
    api
      .listUnmanagedObjects(kind, id)
      .then(setItems)
      .catch((e) => {
        setItems([]);
        setError(errorMessage(e, "adopt.loadFailed"));
      });
  }

  useEffect(() => {
    if (hostId) {
      setResult(null);
      load(hostId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId]);

  const shown = useMemo(
    () => (items ?? []).filter((i) => !isSystem(i)).sort((a, b) => Number(b.adoptable) - Number(a.adoptable) || Number(b.bulk_adoptable) - Number(a.bulk_adoptable)),
    [items]
  );
  const systemCount = (items ?? []).filter(isSystem).length;
  const bulkCount = shown.filter((i) => i.bulk_adoptable).length;

  function reasonText(code: string, details: Record<string, string>): string {
    return t(`adopt.reasons.${code}`, { detail: details[code] ?? "" });
  }

  function describe(item: UnmanagedObject): string {
    const i = item.info;
    switch (kind) {
      case "aliases":
        return [ALIAS_TYPES.includes(i.type) ? t(`aliases.type.${i.type}`) : i.type_label, t("adopt.entries", { count: i.entries })].filter(Boolean).join(" · ");
      case "port-forward":
        return `${i.interface_label} · ${i.protocol} ${endpoint(i.source, i.source_port)} → ${endpoint(i.destination, i.destination_port)} ⇒ ${endpoint(i.target, i.target_port)}`;
      case "one-to-one":
        return `${i.interface_label} · ${i.type.toUpperCase()} · ${i.source} ⇄ ${i.external}${i.destination && i.destination !== "any" ? ` · ${t("adopt.towards", { destination: i.destination })}` : ""}`;
      case "outbound-nat":
        return `${i.interface_label} · ${i.protocol} ${endpoint(i.source, i.source_port)} → ${endpoint(i.destination, i.destination_port)} ⇒ ${i.target ? endpoint(i.target, i.target_port) : t("adopt.interfaceAddress")}`;
    }
  }

  async function adopt(item: UnmanagedObject) {
    setBusy(item.uuid);
    try {
      await api.adoptObject(kind, hostId, item.uuid);
      toast.show(t("adopt.adopted", { name: item.title }), "success");
      onAdopted();
      load(hostId);
    } catch (e) {
      toast.show(errorMessage(e, "adopt.failed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  async function adoptAll() {
    setBusy("all");
    try {
      const done = await api.adoptAllObjects(kind, hostId);
      setResult(done);
      toast.show(t("adopt.allDone", { count: done.adopted.length, skipped: done.skipped.length }), done.adopted.length ? "success" : "info");
      onAdopted();
      load(hostId);
    } catch (e) {
      toast.show(errorMessage(e, "adopt.failed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal title={t(TITLE_KEY[kind])} onClose={onClose}>
      <div className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
        <p className="text-sm text-slate-600">{t("adopt.intro")}</p>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label className="flex items-center gap-2 text-sm text-slate-600">
            {t("adopt.host")}
            <Select className="w-auto" value={hostId} onChange={(e) => setHostId(e.target.value)}>
              {enabledHosts.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </Select>
          </label>
          {bulkCount > 0 && (
            <Button onClick={adoptAll} disabled={busy !== null} loading={busy === "all"}>
              {t("adopt.all", { count: bulkCount })}
            </Button>
          )}
        </div>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {items === null && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
        {items !== null && shown.length === 0 && !error && <p className="text-sm text-slate-500">{t("adopt.none")}</p>}
        {result && (
          <div role="status" className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
            <p className="font-medium">{t("adopt.allDone", { count: result.adopted.length, skipped: result.skipped.length })}</p>
            {result.skipped.length > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-slate-500">
                {result.skipped.map((s) => (
                  <li key={s.uuid}>
                    <span className="font-medium text-slate-700">{s.title}</span>: {s.reasons.map((code) => reasonText(code, s.reason_details)).join(" · ")}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <ul className="space-y-2">
          {shown.map((item) => (
            <li key={item.uuid} className="rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium text-slate-900">
                    {item.title}
                    {!item.enabled && <span className="ml-2 text-xs font-normal text-slate-500">{t("common.statusDisabled")}</span>}
                  </p>
                  <p className="break-words text-xs text-slate-500">{describe(item)}</p>
                  {kind === "aliases" && item.info.content && (
                    <p className="mt-1 whitespace-pre-line break-all font-mono text-xs text-slate-500">{item.info.content}</p>
                  )}
                  {kind === "aliases" && item.info.description && <p className="mt-1 text-xs text-slate-500">{item.info.description}</p>}
                </div>
                {item.adoptable ? (
                  <Button variant="outline" onClick={() => adopt(item)} disabled={busy !== null} loading={busy === item.uuid}>
                    {t("adopt.action")}
                  </Button>
                ) : (
                  <Badge tone={item.bulk_adoptable ? "info" : "neutral"}>
                    {item.bulk_adoptable ? t("adopt.bulkOnly") : t("adopt.notPossible")}
                  </Badge>
                )}
              </div>
              {!item.adoptable && (
                <p className="mt-2 text-xs text-slate-500">{item.reasons.map((code) => reasonText(code, item.reason_details)).join(" · ")}</p>
              )}
              {item.kept.length > 0 && (
                <p className="mt-2 text-xs text-slate-500">
                  {t("adopt.kept", { fields: item.kept.map((field) => t(`adopt.keptFields.${field}`)).join(", ") })}
                </p>
              )}
            </li>
          ))}
        </ul>
        {systemCount > 0 && <p className="text-xs text-slate-500">{t("adopt.systemNote", { count: systemCount })}</p>}
        <div className="flex justify-end">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.close")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
