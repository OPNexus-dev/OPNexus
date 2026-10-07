"use client";

import { HostMaintenance } from "@/components/HostMaintenance";
import { HostResourceUsage } from "@/components/HostResourceUsage";
import { HostServices } from "@/components/HostServices";
import { RestoreWizard } from "@/components/RestoreWizard";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { Spinner } from "@/components/ui/Spinner";
import { StatusDot } from "@/components/ui/StatusDot";
import { api, AliasOption, Host, HostBackup, LiveRule, PolicyAlias, RestoreOperation, Rule } from "@/lib/api";
import { ArrowLeft, Download, ExternalLink, RotateCcw, ShieldCheck, ShieldOff } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Collapsible } from "@/components/ui/Collapsible";
import { useEffect, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";

export default function HostDetailPage() {
  const { t, ts, errorMessage, locale } = useLanguage();
  const { id } = useParams<{ id: string }>();
  const [host, setHost] = useState<Host | null>(null);
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [liveRules, setLiveRules] = useState<LiveRule[] | null>(null);
  const [liveRulesError, setLiveRulesError] = useState<string | null>(null);
  const [aliases, setAliases] = useState<PolicyAlias[] | null>(null);
  const [liveAliases, setLiveAliases] = useState<AliasOption[] | null>(null);
  const [liveAliasesError, setLiveAliasesError] = useState<string | null>(null);
  const [backups, setBackups] = useState<HostBackup[] | null>(null);
  const [backupsError, setBackupsError] = useState<string | null>(null);
  const [triggeringBackup, setTriggeringBackup] = useState(false);
  const [restoreTarget, setRestoreTarget] = useState<string | null>(null);
  const [restoreHistory, setRestoreHistory] = useState<RestoreOperation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function loadRestoreHistory() {
    api.listRestoreOperations(id).then(setRestoreHistory).catch(() => {});
  }

  useEffect(() => {
    api.getHost(id).then(setHost).catch((e) => setError(errorMessage(e, "hostDetail.notFound")));
    api.listRules().then(setRules).catch(() => {});
    api.listAliases().then(setAliases).catch(() => {});
    api
      .getLiveRules(id)
      .then(setLiveRules)
      .catch((e) => setLiveRulesError(errorMessage(e, "hostDetail.liveRulesFailed")));
    api
      .getHostAliases(id)
      .then(setLiveAliases)
      .catch((e) => setLiveAliasesError(errorMessage(e, "hostDetail.liveAliasesFailed")));
    api
      .listHostBackups(id)
      .then(setBackups)
      .catch((e) => setBackupsError(errorMessage(e, "hostDetail.backupsFailed")));
    loadRestoreHistory();
  }, [id]);

  async function handleTriggerBackup() {
    setTriggeringBackup(true);
    setBackupsError(null);
    try {
      const created = await api.triggerHostBackup(id);
      setBackups((prev) => [created, ...(prev ?? [])]);
    } catch (e) {
      setBackupsError(errorMessage(e, "hostDetail.backupFailed"));
    } finally {
      setTriggeringBackup(false);
    }
  }

  async function handleDownloadBackup(backupId: string) {
    try {
      const { blob, filename } = await api.downloadHostBackup(id, backupId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setBackupsError(errorMessage(e, "hostDetail.downloadFailed"));
    }
  }

  function formatSize(bytes: number): string {
    return `${(bytes / 1024).toFixed(0)} KB`;
  }

  // effective_host_ids (not target_host_ids) so this also picks up rules/
  // aliases that reach this host only via a device group, not just ones
  // directly assigned to it.
  const managedRules = rules?.filter((r) => r.effective_host_ids.includes(id)) ?? [];
  const managedAliases = aliases?.filter((a) => a.effective_host_ids.includes(id)) ?? [];

  return (
    <div>
      <Link href="/hosts" className="mb-4 inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700">
        <ArrowLeft size={14} />
        {t("hostDetail.backToHosts")}
      </Link>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {!host && !error && (
        <div className="mb-8">
          <div className="mb-3 flex items-center gap-3">
            <Skeleton className="h-7 w-40" />
            <Skeleton className="h-5 w-16 rounded-full" />
          </div>
          <Skeleton className="mb-6 h-4 w-56" />
          <Skeleton className="h-5 w-56" />
        </div>
      )}

      {host && (
        <>
          <div className="mb-6 flex items-center gap-3">
            <h1 className="text-2xl font-semibold text-slate-900">{host.name}</h1>
            {host.carp_status && (
              <span
                className={
                  host.carp_status === "MASTER"
                    ? "rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700"
                    : "rounded-full bg-flame-100 px-2.5 py-0.5 text-xs font-medium text-flame-700"
                }
              >
                {host.carp_status}
              </span>
            )}
            {host.cluster_name && (
              <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-600">
                {host.cluster_name}
              </span>
            )}
          </div>
          <div className="mb-6 flex items-center gap-4 text-sm text-slate-500">
            <span>{host.base_url}</span>
            <StatusDot status={host.status} />
            <Button href={host.base_url} target="_blank" rel="noopener noreferrer" variant="ghost">
              <ExternalLink size={14} />
              {t("hosts.openInOpnsense")}
            </Button>
          </div>

          <HostResourceUsage host={host} />

          <HostMaintenance host={host} />

          <h2 className="mb-3 text-lg font-semibold text-slate-900">{t("hostDetail.managedRules")}</h2>
          <Card className="mb-8 overflow-x-auto">
            {!rules ? (
              <div className="space-y-3 p-5">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
              </div>
            ) : managedRules.length === 0 ? (
              <p className="px-5 py-6 text-center text-slate-400">{t("hostDetail.noManagedRules")}</p>
            ) : (
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium">{t("common.description")}</th>
                    <th className="px-5 py-3 font-medium">{t("hostDetail.colRule")}</th>
                    <th className="px-5 py-3 font-medium">{t("hostDetail.colStatusOnHost")}</th>
                  </tr>
                </thead>
                <tbody>
                  {managedRules.map((rule) => {
                    const dep = rule.deployments.find((d) => d.host_id === id);
                    return (
                      <tr key={rule.id} className="border-b border-slate-100 last:border-0">
                        <td className="px-5 py-3 font-medium text-slate-900">{rule.description}</td>
                        <td className="px-5 py-3 text-slate-500">
                          {rule.interface} · {rule.direction === "in" ? t("rules.dirIn") : t("rules.dirOut")} · {rule.protocol} ·{" "}
                          {rule.source_net} → {rule.destination_net}
                          {rule.destination_port ? `:${rule.destination_port}` : ""}
                        </td>
                        <td className="px-5 py-3">
                          {dep ? (
                            <span className="text-slate-600">
                              {dep.status}
                              {dep.message && <span className="text-slate-400"> — {ts(dep.message)}</span>}
                            </span>
                          ) : (
                            <span className="text-slate-300">{t("hostDetail.notDeployed")}</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>

          <Collapsible title={t("hostDetail.liveRulesTitle")} count={liveRules?.length ?? null} className="mb-8">
            <Card className="max-h-[70vh] overflow-auto">
            {liveRulesError && <p className="px-5 py-6 text-sm text-red-600">{liveRulesError}</p>}
            {!liveRulesError && !liveRules && (
              <div className="space-y-3 p-5">
                <div className="mb-2 flex items-center gap-2 text-sm text-slate-400">
                  <Spinner size={14} />
                  {t("hostDetail.fetchingRules")}
                </div>
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-4 w-full" />
                ))}
              </div>
            )}
            {liveRules && (
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
                    <th className="px-5 py-3 font-medium">{t("common.description")}</th>
                    <th className="px-5 py-3 font-medium">{t("logs.interface")}</th>
                    <th className="px-5 py-3 font-medium">{t("fields.direction")}</th>
                    <th className="px-5 py-3 font-medium">{t("nat.protocol")}</th>
                    <th className="px-5 py-3 font-medium">{t("drift.colSourceDest")}</th>
                    <th className="px-5 py-3 font-medium">{t("logs.action")}</th>
                    <th className="px-5 py-3 font-medium">{t("hostDetail.colOrigin")}</th>
                  </tr>
                </thead>
                <tbody>
                  {liveRules.map((rule) => (
                    <tr key={rule.uuid} className="border-b border-slate-100 last:border-0">
                      <td className="px-5 py-3">
                        {rule.enabled ? (
                          <ShieldCheck size={15} className="text-emerald-500" />
                        ) : (
                          <ShieldOff size={15} className="text-slate-300" />
                        )}
                      </td>
                      <td className="px-5 py-3 text-slate-900">{rule.description}</td>
                      <td className="px-5 py-3 text-slate-500">{rule.interface_label ?? "—"}</td>
                      <td className="px-5 py-3 text-slate-500">{rule.direction_label}</td>
                      <td className="px-5 py-3 text-slate-500">{rule.protocol_label ?? "any"}</td>
                      <td className="px-5 py-3 text-slate-500">
                        {rule.source_net}
                        {rule.source_port ? `:${rule.source_port}` : ""} → {rule.destination_net}
                        {rule.destination_port ? `:${rule.destination_port}` : ""}
                      </td>
                      <td className="px-5 py-3">
                        <span
                          className={
                            rule.action === "pass"
                              ? "rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700"
                              : "rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700"
                          }
                        >
                          {rule.action_label}
                        </span>
                      </td>
                      <td className="px-5 py-3">
                        {rule.is_automatic ? (
                          <span className="text-xs text-slate-400">{t("hostDetail.automatic")}</span>
                        ) : (
                          <span className="text-xs text-slate-500">{t("hostDetail.custom")}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
          </Collapsible>

          <h2 className="mb-3 mt-8 text-lg font-semibold text-slate-900">{t("hostDetail.managedAliases")}</h2>
          <Card className="mb-8 overflow-x-auto">
            {!aliases ? (
              <div className="space-y-3 p-5">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
              </div>
            ) : managedAliases.length === 0 ? (
              <p className="px-5 py-6 text-center text-slate-400">{t("hostDetail.noManagedAliases")}</p>
            ) : (
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium">{t("hosts.name")}</th>
                    <th className="px-5 py-3 font-medium">{t("aliases.content")}</th>
                    <th className="px-5 py-3 font-medium">{t("hostDetail.colStatusOnHost")}</th>
                  </tr>
                </thead>
                <tbody>
                  {managedAliases.map((alias) => {
                    const dep = alias.deployments.find((d) => d.host_id === id);
                    return (
                      <tr key={alias.id} className="border-b border-slate-100 last:border-0">
                        <td className="px-5 py-3 font-mono font-medium text-slate-900">{alias.name}</td>
                        <td className="px-5 py-3 whitespace-pre-wrap text-slate-500">{alias.content || t("aliases.empty")}</td>
                        <td className="px-5 py-3">
                          {dep ? (
                            <span className="text-slate-600">
                              {dep.status}
                              {dep.message && <span className="text-slate-400"> — {ts(dep.message)}</span>}
                            </span>
                          ) : (
                            <span className="text-slate-300">{t("hostDetail.notDeployed")}</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>

          <Collapsible title={t("hostDetail.liveAliasesTitle")} count={liveAliases?.length ?? null} className="mb-8">
            <Card className="max-h-[70vh] overflow-auto">
            {liveAliasesError && <p className="px-5 py-6 text-sm text-red-600">{liveAliasesError}</p>}
            {!liveAliasesError && !liveAliases && (
              <div className="space-y-3 p-5">
                <div className="mb-2 flex items-center gap-2 text-sm text-slate-400">
                  <Spinner size={14} />
                  {t("hostDetail.fetchingAliases")}
                </div>
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-4 w-full" />
                ))}
              </div>
            )}
            {liveAliases && (
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
                    <th className="px-5 py-3 font-medium">{t("hosts.name")}</th>
                    <th className="px-5 py-3 font-medium">{t("aliases.typeLabel")}</th>
                    <th className="px-5 py-3 font-medium">{t("aliases.content")}</th>
                    <th className="px-5 py-3 font-medium">{t("common.description")}</th>
                  </tr>
                </thead>
                <tbody>
                  {liveAliases.map((alias) => (
                    <tr key={alias.name} className="border-b border-slate-100 last:border-0">
                      <td className="px-5 py-3">
                        {alias.enabled ? (
                          <ShieldCheck size={15} className="text-emerald-500" />
                        ) : (
                          <ShieldOff size={15} className="text-slate-300" />
                        )}
                      </td>
                      <td className="px-5 py-3 font-mono text-slate-900">{alias.name}</td>
                      <td className="px-5 py-3 text-slate-500">{alias.type}</td>
                      <td className="px-5 py-3 whitespace-pre-wrap text-slate-500">{alias.content || t("aliases.empty")}</td>
                      <td className="px-5 py-3 text-slate-500">{alias.description || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
          </Collapsible>

          <div className="mb-3 mt-8 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-slate-900">{t("hostDetail.backups")}</h2>
            <Button onClick={handleTriggerBackup} disabled={triggeringBackup}>
              {triggeringBackup && <Spinner size={14} />}
              {t("hostDetail.backupNow")}
            </Button>
          </div>
          <Card className="overflow-x-auto">
            {backupsError && <p className="px-5 py-4 text-sm text-red-600">{backupsError}</p>}
            {!backupsError && !backups && (
              <div className="space-y-3 p-5">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
              </div>
            )}
            {backups && backups.length === 0 && (
              <p className="px-5 py-6 text-center text-slate-400">{t("hostDetail.noBackups")}</p>
            )}
            {backups && backups.length > 0 && (
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-5 py-3 font-medium">{t("hostDetail.colTime")}</th>
                    <th className="px-5 py-3 font-medium">{t("hostDetail.colSize")}</th>
                    <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
                  </tr>
                </thead>
                <tbody>
                  {backups.map((backup) => (
                    <tr key={backup.id} className="border-b border-slate-100 last:border-0">
                      <td className="px-5 py-3 text-slate-900">{new Date(backup.created_at).toLocaleString(locale)}</td>
                      <td className="px-5 py-3 text-slate-500">{formatSize(backup.size)}</td>
                      <td className="px-5 py-3 text-right">
                        <Button variant="ghost" onClick={() => handleDownloadBackup(backup.id)}>
                          <Download size={14} />
                          {t("hostDetail.download")}
                        </Button>
                        <Button variant="ghost" onClick={() => setRestoreTarget(backup.id)}>
                          <RotateCcw size={14} />
                          {t("hostDetail.restore")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <HostServices host={host} />

          {restoreHistory && restoreHistory.length > 0 && (
            <>
              <h2 className="mb-3 mt-8 text-lg font-semibold text-slate-900">{t("hostDetail.restoreHistory")}</h2>
              <Card className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-5 py-3 font-medium">{t("hostDetail.colTime")}</th>
                      <th className="px-5 py-3 font-medium">{t("hostDetail.colBackup")}</th>
                      <th className="px-5 py-3 font-medium">{t("hosts.status")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {restoreHistory.map((op) => (
                      <tr key={op.id} className="border-b border-slate-100 last:border-0">
                        <td className="px-5 py-3 text-slate-900">{new Date(op.created_at).toLocaleString(locale)}</td>
                        <td className="px-5 py-3 text-slate-500">{op.backup_id}</td>
                        <td className="px-5 py-3">
                          {op.status === "verified" && <span className="text-emerald-700">{t("hostDetail.restoreVerified")}</span>}
                          {op.status === "handed_off" && <span className="text-flame-700">{t("hostDetail.restoreHandedOff")}</span>}
                          {op.status === "cancelled" && <span className="text-slate-500">{t("hostDetail.restoreCancelled")}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </>
          )}
        </>
      )}
      {restoreTarget && (
        <RestoreWizard
          hostId={id}
          hostName={host?.name ?? ""}
          backupId={restoreTarget}
          onClose={() => setRestoreTarget(null)}
          onChanged={loadRestoreHistory}
        />
      )}
    </div>
  );
}
