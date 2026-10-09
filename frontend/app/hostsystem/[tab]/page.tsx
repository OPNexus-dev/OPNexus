"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { NetworkChangeModal } from "@/components/NetworkChangeModal";
import { NetworkObjectSection, ObjectChange } from "@/components/NetworkObjectSection";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Input } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { Table, TBody, TD, TH, THead, TRow } from "@/components/ui/Table";
import { api, Host, HostSnapshot, NetworkKind } from "@/lib/api";
import { useReadOnly } from "@/lib/useReadOnly";
import { HardDrive, Plus, Trash2 } from "lucide-react";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

const HOST_KEY = "opnexus-network-host"; // shared with the network pages: one firewall stays selected
const KIND_BY_TAB: Record<string, NetworkKind> = { tunables: "tunable", cron: "cron_job", syslog: "syslog_destination" };

/** Settings that live on the firewall itself: tunables, cron jobs, remote syslog targets and ZFS snapshots. */
export default function HostSystemPage() {
  const { t, errorMessage } = useLanguage();
  const { tab } = useParams<{ tab: string }>();
  const section = ["tunables", "cron", "syslog", "snapshots"].includes(tab) ? tab : "tunables";
  const readOnly = useReadOnly();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [hostId, setHostId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [change, setChange] = useState<ObjectChange | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    api
      .listHosts()
      .then((list) => {
        setHosts(list);
        let remembered = "";
        try { remembered = sessionStorage.getItem(HOST_KEY) ?? ""; } catch { /* storage may be blocked */ }
        setHostId(list.find((h) => h.id === remembered)?.id ?? list[0]?.id ?? "");
      })
      .catch((e) => setError(errorMessage(e, "network.loadFailed")));
  }, [errorMessage]);

  if (hosts && hosts.length === 0) {
    return (
      <div>
        <h1 className="mb-6 text-2xl font-semibold text-slate-900">{t(`nav.items.hostsystem-${section}`)}</h1>
        <EmptyState icon={HardDrive} title={t("drift.noHosts")} />
      </div>
    );
  }
  const host = hosts?.find((h) => h.id === hostId);
  const kind = KIND_BY_TAB[section];

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900">{t(`nav.items.hostsystem-${section}`)}</h1>
        {hosts && (
          <label className="flex items-center gap-2 text-sm text-slate-600">
            {t("network.host")}
            <select
              value={hostId}
              onChange={(e) => {
                setHostId(e.target.value);
                try { sessionStorage.setItem(HOST_KEY, e.target.value); } catch { /* ignore */ }
              }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm"
            >
              {hosts.map((h) => (
                <option key={h.id} value={h.id}>{h.name}</option>
              ))}
            </select>
          </label>
        )}
      </div>
      <p className="mb-4 text-sm text-slate-600">{t(`hostsystem.intro.${section}`)}</p>
      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}
      {!hostId && !error && <Skeleton className="h-48 w-full" />}
      {hostId && (
        <Card className="overflow-x-auto">
          {kind ? (
            <NetworkObjectSection
              hostId={hostId}
              kind={kind}
              title={t(`nav.items.hostsystem-${section}`)}
              reloadKey={reloadKey}
              readOnly={readOnly}
              onChange={setChange}
            />
          ) : (
            <Snapshots hostId={hostId} readOnly={readOnly} />
          )}
        </Card>
      )}
      {change && host && (
        <NetworkChangeModal
          key={`${hostId}:${change.kind}:${change.action}:${change.uuid ?? ""}`}
          hostId={hostId}
          hostName={host.name}
          kind={change.kind}
          action={change.action}
          uuid={change.uuid}
          preset={change.preset}
          onClose={() => setChange(null)}
          onDone={() => setReloadKey((k) => k + 1)}
        />
      )}
    </div>
  );
}

function Snapshots({ hostId, readOnly }: { hostId: string; readOnly: boolean }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [data, setData] = useState<{ supported: boolean; rows: HostSnapshot[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  function load() {
    setData(null);
    api.listSnapshots(hostId).then(setData).catch((e) => setError(errorMessage(e, "network.loadFailed")));
  }
  useEffect(load, [hostId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      await api.createSnapshot(hostId, name.trim(), note.trim());
      toast.show(t("hostsystem.snapshots.created", { name: name.trim() }), "success");
      setShowCreate(false);
      setName("");
      setNote("");
      load();
    } catch (err) {
      setFormError(errorMessage(err, "hostsystem.snapshots.createFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(row: HostSnapshot) {
    if (!confirm(t("hostsystem.snapshots.deleteConfirm", { name: row.name }))) return;
    try {
      await api.deleteSnapshot(hostId, row.uuid);
      toast.show(t("hostsystem.snapshots.deleted", { name: row.name }), "success");
      load();
    } catch (err) {
      toast.show(errorMessage(err, "hostsystem.snapshots.deleteFailed"), "danger");
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
        <h2 className="text-sm font-semibold text-slate-700">{t("nav.items.hostsystem-snapshots")}</h2>
        <Button variant="outline" disabled={readOnly || data?.supported === false} onClick={() => setShowCreate(true)}>
          <Plus size={16} />
          {t("hostsystem.snapshots.create")}
        </Button>
      </div>
      {error && <p className="px-5 py-3 text-sm text-red-600">{error}</p>}
      {!data && !error && <p className="px-5 py-4 text-sm text-slate-500">{t("common.loading")}</p>}
      {data && !data.supported && <p className="px-5 py-4 text-sm text-slate-500">{t("hostsystem.snapshots.unsupported")}</p>}
      {data && data.supported && (
        <Table>
          <THead>
            <TRow>
              <TH>{t("hostsystem.snapshots.name")}</TH>
              <TH>{t("hostsystem.snapshots.state")}</TH>
              <TH>{t("hostsystem.snapshots.size")}</TH>
              <TH>{t("hostsystem.snapshots.createdAt")}</TH>
              <TH><span className="sr-only">{t("common.actions")}</span></TH>
            </TRow>
          </THead>
          <TBody>
            {data.rows.map((row) => (
              <TRow key={row.uuid}>
                <TD className="font-mono text-xs">{row.name}</TD>
                <TD>{row.active.includes("N") ? t("hostsystem.snapshots.activeNow") : row.active.includes("R") ? t("hostsystem.snapshots.activeNext") : "–"}</TD>
                <TD>{row.size || "–"}</TD>
                <TD>{row.created || "–"}</TD>
                <TD>
                  <fieldset disabled={readOnly} className="contents">
                    <div className="flex justify-end">
                      {(row.active === "-" || row.active === "") && (
                        <Button variant="dangerSubtle" aria-label={t("network.rowDelete")} onClick={() => remove(row)}>
                          <Trash2 size={14} />
                        </Button>
                      )}
                    </div>
                  </fieldset>
                </TD>
              </TRow>
            ))}
          </TBody>
        </Table>
      )}
      {showCreate && (
        <Modal title={t("hostsystem.snapshots.create")} onClose={() => setShowCreate(false)}>
          <form onSubmit={create} className="space-y-4">
            <p className="text-sm text-slate-600">{t("hostsystem.snapshots.createHint")}</p>
            <FieldGroup label={t("hostsystem.snapshots.name")} hint={t("hostsystem.snapshots.nameHint")}>
              <Input required value={name} onChange={(e) => setName(e.target.value)} pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,63}" autoComplete="off" />
            </FieldGroup>
            <FieldGroup label={t("hostsystem.snapshots.note")}>
              <Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} />
            </FieldGroup>
            {formError && <p role="alert" className="text-sm text-red-600">{formError}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setShowCreate(false)}>{t("common.cancel")}</Button>
              <Button type="submit" disabled={busy} loading={busy}>{t("hostsystem.snapshots.create")}</Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
