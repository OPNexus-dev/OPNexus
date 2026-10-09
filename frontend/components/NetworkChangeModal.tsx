"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { FieldGroup, Input, Select } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import {
  api,
  NetworkAction,
  NetworkChangePreview,
  NetworkChangeResult,
  NetworkKind,
  NetworkTemplate,
} from "@/lib/api";
import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";

type FieldType = "text" | "select" | "check" | "password";
interface FieldDef {
  name: string;
  type: FieldType;
  // Only shown for these values of another field (e.g. CARP-only settings).
  when?: { field: string; value: string | string[] };
  // i18n key of a short note shown next to the label
  hint?: string;
  // The firewall only accepts values matching this; checked here so the error comes before the apply step.
  pattern?: RegExp;
}

const FIELDS: Record<NetworkKind, FieldDef[]> = {
  vlan: [
    { name: "if", type: "select" },
    { name: "tag", type: "text" },
    { name: "pcp", type: "select" },
    { name: "descr", type: "text" },
  ],
  vip: [
    { name: "interface", type: "select" },
    { name: "mode", type: "select" },
    { name: "network", type: "text" },
    { name: "vhid", type: "text", when: { field: "mode", value: "carp" } },
    { name: "advbase", type: "text", when: { field: "mode", value: "carp" } },
    { name: "advskew", type: "text", when: { field: "mode", value: "carp" } },
    { name: "password", type: "password", when: { field: "mode", value: "carp" } },
    { name: "descr", type: "text" },
  ],
  gateway: [
    { name: "name", type: "text" },
    { name: "interface", type: "select" },
    { name: "ipprotocol", type: "select" },
    { name: "gateway", type: "text" },
    { name: "defaultgw", type: "check" },
    { name: "monitor_disable", type: "check" },
    { name: "priority", type: "text" },
    { name: "disabled", type: "check" },
    { name: "descr", type: "text" },
  ],
  interface: [
    { name: "if", type: "select" },
    { name: "descr", type: "text", hint: "network.hint.interfaceDescr", pattern: /^[A-Za-z0-9_]{1,255}$/ },
    { name: "enable", type: "check" },
    { name: "type4", type: "select" },
    { name: "ipaddr", type: "text", when: { field: "type4", value: "staticv4" } },
    { name: "gateway", type: "select", when: { field: "type4", value: "staticv4" } },
    { name: "type6", type: "select" },
    { name: "ipaddrv6", type: "text", when: { field: "type6", value: "staticv6" } },
    { name: "gatewayv6", type: "select", when: { field: "type6", value: "staticv6" } },
    { name: "mtu", type: "text" },
    { name: "blockpriv", type: "check" },
    { name: "blockbogons", type: "check" },
    { name: "lock", type: "check" },
  ],
  dns_host: [
    { name: "enabled", type: "check" },
    { name: "hostname", type: "text" },
    { name: "domain", type: "text" },
    { name: "rr", type: "select" },
    { name: "server", type: "text", when: { field: "rr", value: ["A", "AAAA"] } },
    { name: "mx", type: "text", when: { field: "rr", value: "MX" } },
    { name: "mxprio", type: "text", when: { field: "rr", value: "MX" } },
    { name: "addptr", type: "check" },
    { name: "description", type: "text" },
  ],
  dhcp_host: [
    { name: "host", type: "text" },
    { name: "domain", type: "text" },
    { name: "ip", type: "text" },
    { name: "hwaddr", type: "text" },
    { name: "client_id", type: "text" },
    { name: "lease_time", type: "text" },
    { name: "local", type: "check" },
    { name: "ignore", type: "check" },
    { name: "descr", type: "text" },
  ],
  kea_subnet: [
    { name: "subnet", type: "text" },
    { name: "pools", type: "text" },
    { name: "option_data.routers", type: "text" },
    { name: "option_data.domain_name_servers", type: "text" },
    { name: "option_data.domain_name", type: "text" },
    { name: "option_data.ntp_servers", type: "text" },
    { name: "valid_lifetime", type: "text" },
    { name: "next_server", type: "text" },
    { name: "description", type: "text" },
  ],
  kea_reservation: [
    { name: "subnet", type: "select" },
    { name: "ip_address", type: "text" },
    { name: "hw_address", type: "text" },
    { name: "client_id", type: "text" },
    { name: "hostname", type: "text" },
    { name: "next_server", type: "text" },
    { name: "description", type: "text" },
  ],
  tunable: [
    { name: "tunable", type: "text" },
    { name: "value", type: "text" },
    { name: "descr", type: "text" },
  ],
  cron_job: [
    { name: "enabled", type: "check" },
    { name: "minutes", type: "text" },
    { name: "hours", type: "text" },
    { name: "days", type: "text" },
    { name: "months", type: "text" },
    { name: "weekdays", type: "text" },
    { name: "who", type: "text" },
    { name: "command", type: "select" },
    { name: "parameters", type: "text" },
    { name: "description", type: "text" },
  ],
  syslog_destination: [
    { name: "enabled", type: "check" },
    { name: "transport", type: "select" },
    { name: "hostname", type: "text" },
    { name: "port", type: "text" },
    { name: "program", type: "text" },
    { name: "level", type: "text" },
    { name: "facility", type: "text" },
    { name: "rfc5424", type: "check" },
    { name: "description", type: "text" },
  ],
  gateway_group: [
    { name: "name", type: "text" },
    { name: "item", type: "text" },
    { name: "item2", type: "text" },
    { name: "item3", type: "text" },
    { name: "item4", type: "text" },
    { name: "item5", type: "text" },
    { name: "trigger", type: "select" },
    { name: "poolopts", type: "select" },
    { name: "descr", type: "text" },
  ],
  lagg: [
    { name: "members", type: "text" },
    { name: "proto", type: "select" },
    { name: "primary_member", type: "text" },
    { name: "lagghash", type: "select" },
    { name: "mtu", type: "text" },
    { name: "descr", type: "text" },
  ],
  bridge: [
    { name: "members", type: "text" },
    { name: "proto", type: "select" },
    { name: "enablestp", type: "check" },
    { name: "linklocal", type: "check" },
    { name: "descr", type: "text" },
  ],
  loopback: [
    { name: "deviceId", type: "text" },
    { name: "description", type: "text" },
  ],
  gre: [
    { name: "local-addr", type: "text" },
    { name: "remote-addr", type: "text" },
    { name: "tunnel-local-addr", type: "text" },
    { name: "tunnel-remote-addr", type: "text" },
    { name: "tunnel-remote-net", type: "text" },
    { name: "descr", type: "text" },
  ],
  dns_forward: [
    { name: "enabled", type: "check" },
    { name: "type", type: "select" },
    { name: "domain", type: "text" },
    { name: "server", type: "text" },
    { name: "port", type: "text" },
    { name: "forward_tcp_upstream", type: "check" },
    { name: "forward_first", type: "check" },
    { name: "description", type: "text" },
  ],
  dhcp_range: [
    { name: "interface", type: "select" },
    { name: "start_addr", type: "text" },
    { name: "end_addr", type: "text" },
    { name: "subnet_mask", type: "text" },
    { name: "lease_time", type: "text" },
    { name: "domain_type", type: "select" },
    { name: "domain", type: "text" },
    { name: "description", type: "text" },
  ],
  route: [
    { name: "network", type: "text" },
    { name: "gateway", type: "select" },
    { name: "enabled", type: "check" },
    { name: "descr", type: "text" },
  ],
};

interface Props {
  hostId: string;
  hostName: string;
  kind: NetworkKind;
  action: NetworkAction;
  uuid?: string;
  /** Values to start a new object with (e.g. the device being assigned). */
  preset?: Record<string, string>;
  /** Other nodes of the same cluster; after an add the same object can be created there too. */
  peers?: { id: string; name: string }[];
  onTransfer?: (peer: { id: string; name: string }, fields: Record<string, string>) => void;
  onClose: () => void;
  onDone: () => void;
}

type Step = "form" | "review" | "done";

function isVisible(f: FieldDef, values: Record<string, string>): boolean {
  if (!f.when) return true;
  const wanted = Array.isArray(f.when.value) ? f.when.value : [f.when.value];
  return wanted.includes(values[f.when.field] ?? "");
}

export function NetworkChangeModal({ hostId, hostName, kind, action, uuid, preset, peers, onTransfer, onClose, onDone }: Props) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [step, setStep] = useState<Step>(action === "del" ? "review" : "form");
  const [template, setTemplate] = useState<NetworkTemplate | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  // What the firewall holds right now (edit only) — the baseline for "what did the user change".
  const [initial, setInitial] = useState<Record<string, string>>({});
  const [plan, setPlan] = useState<NetworkChangePreview | null>(null);
  const [result, setResult] = useState<NetworkChangeResult | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [typedHost, setTypedHost] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  // The form starts from what the firewall itself offers for a new object (defaults + valid choices).
  useEffect(() => {
    if (action === "del") return;
    Promise.all([
      api.getNetworkTemplate(hostId, kind),
      action === "set" && uuid ? api.getNetworkItem(hostId, kind, uuid) : Promise.resolve({} as Record<string, string>),
    ])
      .then(([tpl, current]) => {
        setTemplate(tpl);
        const base = Object.fromEntries(Object.entries(tpl).map(([k, v]) => [k, v.value]));
        setInitial(current);
        setValues({ ...base, ...(preset ?? {}), ...current });
      })
      .catch((e) => setError(errorMessage(e, "network.change.loadFailed")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId, kind, action]);

  useEffect(() => {
    if (action === "del") void loadPreview({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** For an edit only the fields that really changed are sent. */
  function changedFields(): Record<string, string> {
    const visible = FIELDS[kind].filter((f) => isVisible(f, values));
    const out: Record<string, string> = {};
    for (const f of visible) {
      const current = values[f.name] ?? "";
      if (action === "set" && f.type !== "password" && current === (initial[f.name] ?? "")) continue;
      if (f.type === "password" && !current) continue;
      if (action === "add" && current === "" && f.type !== "check") continue;
      out[f.name] = current;
    }
    return out;
  }

  async function loadPreview(fields: Record<string, string>) {
    setBusy(true);
    setError(null);
    try {
      setPlan(await api.previewNetworkChange(hostId, { kind, action, uuid, fields }));
      setStep("review");
    } catch (e) {
      setError(errorMessage(e, "network.change.previewFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      const fields = action === "del" ? {} : changedFields();
      setSent(fields);
      const res = await api.executeNetworkChange(hostId, { kind, action, uuid, fields }, plan.token, acknowledged);
      setResult(res);
      setStep("done");
      toast.show(t("network.change.applied"), "success");
      onDone();
    } catch (e) {
      setError(errorMessage(e, "network.change.applyFailed"));
    } finally {
      setBusy(false);
    }
  }

  // First visible field whose value the firewall would refuse (only fields the user typed into).
  const invalidField = FIELDS[kind].find(
    (f) => f.pattern && isVisible(f, values) && (values[f.name] ?? "") !== "" && !f.pattern.test(values[f.name]),
  );
  const needsTypedHost = !!plan?.requires_acknowledgement;
  const canApply = !!plan && plan.can_execute && !busy && (!needsTypedHost || (acknowledged && typedHost === hostName));

  return (
    <Modal title={t(`network.change.title.${action}`, { kind: t(`network.kinds.${kind}`) })} onClose={onClose}>
      <div className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

        {step === "form" && (
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              void loadPreview(changedFields());
            }}
          >
            {!template && !error && <p className="text-sm text-slate-500">{t("common.loading")}</p>}
            {template &&
              FIELDS[kind]
                .filter((f) => template[f.name] !== undefined && isVisible(f, values))
                .map((f) => (
                  <FieldGroup key={f.name} label={t(`network.field.${f.name}`)} hint={f.hint ? t(f.hint) : undefined}>
                    {f.type === "select" ? (
                      <Select value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                        {(template[f.name].options ?? []).map((o) => (
                          <option key={o.value} value={o.value}>{o.label || o.value}</option>
                        ))}
                      </Select>
                    ) : f.type === "check" ? (
                      <label className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={values[f.name] === "1"}
                          onChange={(e) => setValues({ ...values, [f.name]: e.target.checked ? "1" : "0" })}
                        />
                        {t(`network.field.${f.name}Hint`)}
                      </label>
                    ) : (
                      <Input
                        type={f.type === "password" ? "password" : "text"}
                        autoComplete="off"
                        value={values[f.name] ?? ""}
                        placeholder={f.type === "password" && action === "set" ? t("network.change.keepUnchanged") : undefined}
                        onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                      />
                    )}
                  </FieldGroup>
                ))}
            {invalidField && <p role="alert" className="text-sm text-red-600">{t(invalidField.hint ?? "network.change.invalidValue")}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
              <Button type="submit" disabled={!template || busy || !!invalidField} loading={busy}>{t("network.change.toPreview")}</Button>
            </div>
          </form>
        )}

        {step === "review" && plan && (
          <div className="space-y-4">
            {plan.blocks.map((b) => (
              <p key={b.code} className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
                {t(`network.blocks.${b.code}`, b.params)}
              </p>
            ))}
            {plan.risks.length > 0 && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
                <p className="mb-1 flex items-center gap-2 font-medium">
                  <AlertTriangle size={16} aria-hidden /> {t("network.change.riskTitle")}
                </p>
                <ul className="list-disc space-y-1 pl-5">
                  {plan.risks.map((r) => <li key={r.code}>{t(`network.risks.${r.code}`, r.params)}</li>)}
                </ul>
              </div>
            )}
            {plan.warnings.length > 0 && (
              <ul className="list-disc space-y-1 pl-5 text-sm text-slate-600">
                {plan.warnings.map((w) => <li key={w.code}>{t(`network.warnings.${w.code}`, w.params)}</li>)}
              </ul>
            )}
            {plan.changes.length === 0 ? (
              <p className="text-sm text-slate-500">{t("network.change.noChanges")}</p>
            ) : (
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase text-slate-500">
                  <tr>
                    <th className="py-1 pr-3">{t("network.change.col.field")}</th>
                    <th className="py-1 pr-3">{t("network.change.col.before")}</th>
                    <th className="py-1">{t("network.change.col.after")}</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.changes.map((c) => (
                    <tr key={c.field} className="border-t border-slate-100">
                      <td className="py-1 pr-3">{t(`network.field.${c.field}`)}</td>
                      <td className="py-1 pr-3 font-mono text-xs text-slate-500">{c.before ?? "–"}</td>
                      <td className="py-1 font-mono text-xs">{c.after ?? "–"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="text-xs text-slate-500">{t("network.change.backupNote")}</p>
            {needsTypedHost && (
              <div className="space-y-2 rounded-lg border border-slate-200 p-3">
                <label className="flex items-start gap-2 text-sm">
                  <input type="checkbox" className="mt-1" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
                  <span>{t("network.change.ack", { host: hostName })}</span>
                </label>
                <FieldGroup label={t("network.change.typeHost", { host: hostName })}>
                  <Input value={typedHost} onChange={(e) => setTypedHost(e.target.value)} autoComplete="off" />
                </FieldGroup>
              </div>
            )}
            <div className="flex justify-end gap-2">
              {action !== "del" && <Button type="button" variant="ghost" onClick={() => setStep("form")}>{t("network.change.back")}</Button>}
              <Button type="button" variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
              <Button type="button" onClick={apply} disabled={!canApply} loading={busy}>{t("network.change.apply")}</Button>
            </div>
          </div>
        )}

        {step === "done" && result && (
          <div className="space-y-3">
            <p className="flex items-center gap-2 text-sm">
              <Badge tone={result.verified ? "success" : "warning"}>
                {result.verified ? t("network.change.verified") : t("network.change.notVerified")}
              </Badge>
            </p>
            {!result.verified && (
              <p className="text-sm text-amber-700">
                {result.mismatches.includes("unreadable")
                  ? t("network.change.unreachable", { host: hostName })
                  : t("network.change.mismatch", { fields: result.mismatches.join(", ") })}
              </p>
            )}
            <p className="text-xs text-slate-500">{t("network.change.backupId", { id: result.backup_id })}</p>
            {action === "add" && peers && peers.length > 0 && onTransfer && (
              <div className="space-y-2 rounded-lg border border-slate-200 p-3">
                <p className="text-sm text-slate-600">{t("network.change.transferHint")}</p>
                <div className="flex flex-wrap gap-2">
                  {peers.map((peer) => (
                    <Button key={peer.id} type="button" variant="outline" onClick={() => onTransfer(peer, sent)}>
                      {t("network.change.transfer", { host: peer.name })}
                    </Button>
                  ))}
                </div>
              </div>
            )}
            <div className="flex justify-end">
              <Button type="button" onClick={onClose}>{t("common.close")}</Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
