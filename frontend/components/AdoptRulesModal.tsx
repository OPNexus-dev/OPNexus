"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { useLanguage } from "@/components/LanguageProvider";
import { api, Host, UnmanagedRule } from "@/lib/api";
import { useEffect, useState } from "react";

interface Props {
  hosts: Host[];
  onClose: () => void;
  onAdopted: () => void;
}

function summary(rule: UnmanagedRule): string {
  const port = (value: string) => (value ? `:${value}` : "");
  return `${rule.protocol} ${rule.source_net}${port(rule.source_port)} → ${rule.destination_net}${port(rule.destination_port)}`;
}

/**
 * Lists the rules that exist on a firewall but are not managed by OPNexus yet and
 * lets the operator take them over. Taking over only records the rule — nothing is
 * written to the firewall until the rule is changed and pushed the normal way.
 */
export function AdoptRulesModal({ hosts, onClose, onAdopted }: Props) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const enabledHosts = hosts.filter((h) => h.enabled);
  // Derived rather than initialised once: the host list may still be loading when the dialog opens.
  const [chosenHostId, setHostId] = useState("");
  const hostId = chosenHostId || enabledHosts[0]?.id || "";
  const [rules, setRules] = useState<UnmanagedRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  function load(id: string) {
    setRules(null);
    setError(null);
    api
      .listUnmanagedRules(id)
      .then(setRules)
      .catch((e) => {
        setRules([]);
        setError(errorMessage(e, "rules.adopt.loadFailed"));
      });
  }

  useEffect(() => {
    if (hostId) load(hostId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId]);

  async function adopt(rule: UnmanagedRule) {
    setBusy(rule.uuid);
    try {
      await api.adoptRule(hostId, rule.uuid);
      toast.show(t("rules.adopt.adopted", { name: rule.description }), "success");
      onAdopted();
      load(hostId);
    } catch (e) {
      toast.show(errorMessage(e, "rules.adopt.failed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal title={t("rules.adopt.title")} onClose={onClose}>
      <div className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
        <p className="text-sm text-slate-600">{t("rules.adopt.intro")}</p>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          {t("rules.adopt.host")}
          <select
            value={hostId}
            onChange={(e) => setHostId(e.target.value)}
            className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
          >
            {enabledHosts.map((h) => (
              <option key={h.id} value={h.id}>
                {h.name}
              </option>
            ))}
          </select>
        </label>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {rules === null && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
        {rules !== null && rules.length === 0 && !error && (
          <p className="text-sm text-slate-500">{t("rules.adopt.none")}</p>
        )}
        <ul className="space-y-2">
          {(rules ?? []).map((rule) => (
            <li key={rule.uuid} className="rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium text-slate-900">{rule.description}</p>
                  <p className="text-xs text-slate-500">
                    {rule.interface_label} · {rule.direction} · {rule.action} · {summary(rule)}
                    {!rule.enabled ? ` · ${t("common.statusDisabled")}` : ""}
                    {rule.log ? ` · ${t("rules.logPackets")}` : ""}
                  </p>
                </div>
                {rule.adoptable ? (
                  <Button variant="outline" onClick={() => adopt(rule)} disabled={busy !== null} loading={busy === rule.uuid}>
                    {t("rules.adopt.action")}
                  </Button>
                ) : (
                  <Badge tone="neutral">{t("rules.adopt.notPossible")}</Badge>
                )}
              </div>
              {!rule.adoptable && (
                <p className="mt-2 text-xs text-slate-500">
                  {rule.reasons.map((code) => t(`rules.adopt.reasons.${code}`)).join(" · ")}
                </p>
              )}
            </li>
          ))}
        </ul>
        <div className="flex justify-end">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.close")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
