"use client";

import { InfoTip } from "@/components/ui/Tooltip";
import { AcmeAccountForm } from "@/components/AcmeAccountForm";
import { AcmeActionConfirm } from "@/components/AcmeActionConfirm";
import { AcmeCertificateForm } from "@/components/AcmeCertificateForm";
import { CertStatusBadge } from "@/components/CertStatusBadge";
import { ManagedCertificateForm } from "@/components/ManagedCertificateForm";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Spinner } from "@/components/ui/Spinner";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import {
  AcmeAccount,
  AcmeCertificateRequest,
  api,
  DeployStatus,
  Host,
  HostCertificate,
  ManagedCertificate,
} from "@/lib/api";
import { Pencil, Plus, RefreshCw, Rocket, ShieldCheck, Trash2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useReadOnly } from "@/lib/useReadOnly";
import { useLanguage } from "@/components/LanguageProvider";

import { Loading } from "@/components/ui/Loading";
interface HostCertState {
  certs: HostCertificate[];
  error: string | null;
  loading: boolean;
}

interface Row {
  host: Host;
  cert: HostCertificate;
}

const deployStatusTone: Record<DeployStatus, BadgeTone> = {
  draft: "neutral",
  success: "success",
  error: "danger",
};

function DeployStatusBadge({ status }: { status: DeployStatus }) {
  const { t } = useLanguage();
  return <Badge tone={deployStatusTone[status]}>{t(`certs.deploy.${status}`)}</Badge>;
}

type Tab = "live" | "trust" | "acme";

export default function CertificatesPage() {
  const { t } = useLanguage();
  return (
    <Suspense fallback={<Loading>{t("certs.loading")}</Loading>}>
      <CertificatesContent />
    </Suspense>
  );
}

function CertificatesContent() {
  const { t, ts, errorMessage, locale } = useLanguage();
  const readOnly = useReadOnly();
  const router = useRouter();
  const params = useSearchParams();
  const certFilter = params.get("cert") ?? "";
  const [tab, setTab] = useState<Tab>(certFilter ? "trust" : "live");
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [byHost, setByHost] = useState<Record<string, HostCertState>>({});
  const [error, setError] = useState<string | null>(null);

  // Trust-Store state
  const [managedCerts, setManagedCerts] = useState<ManagedCertificate[] | null>(null);
  const [managedError, setManagedError] = useState<string | null>(null);
  const [managedForm, setManagedForm] = useState<{ editing: ManagedCertificate | null } | null>(null);
  const [managedBusy, setManagedBusy] = useState<string | null>(null);

  // ACME state
  const [accounts, setAccounts] = useState<AcmeAccount[] | null>(null);
  const [requests, setRequests] = useState<AcmeCertificateRequest[] | null>(null);
  const [acmeError, setAcmeError] = useState<string | null>(null);
  const [accountForm, setAccountForm] = useState<{ editing: AcmeAccount | null } | null>(null);
  const [requestForm, setRequestForm] = useState<{ editing: AcmeCertificateRequest | null } | null>(null);
  const [acmeBusy, setAcmeBusy] = useState<string | null>(null);
  const [registerTarget, setRegisterTarget] = useState<AcmeAccount | null>(null);
  const [signTarget, setSignTarget] = useState<AcmeCertificateRequest | null>(null);
  const toast = useToast();

  function loadLive() {
    api
      .listHosts()
      .then((list) => {
        setHosts(list);
        setByHost(Object.fromEntries(list.map((h) => [h.id, { certs: [], error: null, loading: true }])));

        for (const host of list) {
          api
            .getHostCertificates(host.id)
            .then((certs) => {
              setByHost((prev) => ({ ...prev, [host.id]: { certs, error: null, loading: false } }));
            })
            .catch((e) => {
              setByHost((prev) => ({
                ...prev,
                [host.id]: { certs: [], error: errorMessage(e, "certs.hostFailed"), loading: false },
              }));
            });
        }
      })
      .catch((e) => setError(errorMessage(e, "certs.hostsLoadFailed")));
  }

  async function loadManaged() {
    try {
      setManagedCerts(await api.listManagedCertificates());
      setManagedError(null);
    } catch (e) {
      setManagedError(errorMessage(e, "certs.loadFailed"));
    }
  }

  async function loadAcme() {
    try {
      const [accountList, requestList] = await Promise.all([api.listAcmeAccounts(), api.listAcmeCertificateRequests()]);
      setAccounts(accountList);
      setRequests(requestList);
      setAcmeError(null);
    } catch (e) {
      setAcmeError(errorMessage(e, "certs.acmeLoadFailed"));
    }
  }

  useEffect(() => {
    loadLive();
    loadManaged();
    loadAcme();
  }, []);

  const rows = useMemo<Row[]>(() => {
    if (!hosts) return [];
    const all: Row[] = [];
    for (const host of hosts) {
      for (const cert of byHost[host.id]?.certs ?? []) {
        all.push({ host, cert });
      }
    }
    all.sort((a, b) => {
      if (a.cert.valid_to === null && b.cert.valid_to === null) return 0;
      if (a.cert.valid_to === null) return 1;
      if (b.cert.valid_to === null) return -1;
      return a.cert.valid_to.localeCompare(b.cert.valid_to);
    });
    return all;
  }, [hosts, byHost]);

  const attentionCount = rows.filter((r) => r.cert.status === "expired" || r.cert.status === "expiring_soon").length;
  const pendingHosts = hosts?.filter((h) => byHost[h.id]?.loading) ?? [];
  const erroredHosts = hosts?.filter((h) => byHost[h.id]?.error) ?? [];
  const hostName = (id: string) => hosts?.find((h) => h.id === id)?.name ?? "?";
  const accountName = (id: string) => accounts?.find((a) => a.id === id)?.name ?? "?";

  async function pushManagedCert(cert: ManagedCertificate) {
    setManagedBusy(cert.id);
    setManagedError(null);
    try {
      await api.pushManagedCertificate(cert.id);
      toast.show(t("certs.certPushed", { name: cert.description }), "success");
      await loadManaged();
    } catch (e) {
      setManagedError(errorMessage(e, "certs.pushFailed"));
    } finally {
      setManagedBusy(null);
    }
  }

  async function deleteManagedCert(cert: ManagedCertificate) {
    if (!confirm(t("certs.certDeleteConfirm", { name: cert.description }))) return;
    setManagedBusy(cert.id);
    setManagedError(null);
    try {
      await api.deleteManagedCertificate(cert.id);
      toast.show(t("certs.certDeleted", { name: cert.description }), "success");
      await loadManaged();
    } catch (e) {
      setManagedError(errorMessage(e, "common.deleteFailed"));
    } finally {
      setManagedBusy(null);
    }
  }

  async function pushAccount(account: AcmeAccount) {
    setAcmeBusy(account.id);
    setAcmeError(null);
    try {
      await api.pushAcmeAccount(account.id);
      toast.show(t("certs.accountPushed", { name: account.name }), "success");
      await loadAcme();
    } catch (e) {
      setAcmeError(errorMessage(e, "certs.pushFailed"));
    } finally {
      setAcmeBusy(null);
    }
  }

  async function deleteAccount(account: AcmeAccount) {
    if (!confirm(t("certs.accountDeleteConfirm", { name: account.name }))) return;
    setAcmeBusy(account.id);
    setAcmeError(null);
    try {
      await api.deleteAcmeAccount(account.id);
      toast.show(t("certs.accountDeleted", { name: account.name }), "success");
      await loadAcme();
    } catch (e) {
      setAcmeError(errorMessage(e, "common.deleteFailed"));
    } finally {
      setAcmeBusy(null);
    }
  }

  async function pushRequest(req: AcmeCertificateRequest) {
    setAcmeBusy(req.id);
    setAcmeError(null);
    try {
      await api.pushAcmeCertificateRequest(req.id);
      toast.show(t("certs.requestPushed", { name: req.name }), "success");
      await loadAcme();
    } catch (e) {
      setAcmeError(errorMessage(e, "certs.pushFailed"));
    } finally {
      setAcmeBusy(null);
    }
  }

  async function pollRequest(req: AcmeCertificateRequest) {
    setAcmeBusy(req.id);
    setAcmeError(null);
    try {
      await api.pollAcmeCertificateRequest(req.id);
      await loadAcme();
    } catch (e) {
      setAcmeError(errorMessage(e, "certs.pollFailed"));
    } finally {
      setAcmeBusy(null);
    }
  }

  async function deleteRequest(req: AcmeCertificateRequest) {
    if (!confirm(t("certs.requestDeleteConfirm", { name: req.name }))) return;
    setAcmeBusy(req.id);
    setAcmeError(null);
    try {
      await api.deleteAcmeCertificateRequest(req.id);
      toast.show(t("certs.requestDeleted", { name: req.name }), "success");
      await loadAcme();
    } catch (e) {
      setAcmeError(errorMessage(e, "common.deleteFailed"));
    } finally {
      setAcmeBusy(null);
    }
  }

  return (
    <div>
      <div className="mb-6 flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
        <div>
          <h1 className="text-xl font-semibold text-slate-900 flex items-center gap-2">{t("certs.title")}<InfoTip text={t("certs.subtitle")} size={18} /></h1>
        </div>
      </div>

      <Tabs
        className="mb-6"
        items={[
          { key: "live", label: t("certs.tabLive") },
          { key: "trust", label: t("certs.tabTrust") },
          { key: "acme", label: t("certs.tabAcme") },
        ]}
        active={tab}
        onChange={(key) => setTab(key as Tab)}
      />

      {tab === "live" && (
        <div>
          {pendingHosts.length > 0 && (
            <span className="mb-4 flex items-center gap-1.5 text-sm text-slate-400">
              <Spinner size={13} />
              {t("certs.hostsPending", { pending: pendingHosts.length, total: hosts?.length ?? 0 })}
            </span>
          )}
          {error && <p className="mb-4 text-sm text-red-600">{error}</p>}
          {attentionCount > 0 && (
            <Card className="mb-4 border-flame-200 bg-flame-50 px-5 py-3 text-sm text-flame-800">
              {t(attentionCount === 1 ? "certs.attentionOne" : "certs.attentionMany", { count: attentionCount })}
            </Card>
          )}
          <Card className="overflow-x-auto" tabIndex={0}>
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-5 py-3 font-medium">{t("certs.colHost")}</th>
                  <th className="px-5 py-3 font-medium">{t("certs.colName")}</th>
                  <th className="px-5 py-3 font-medium">{t("certs.colIssuer")}</th>
                  <th className="px-5 py-3 font-medium">{t("certs.colValidTo")}</th>
                  <th className="px-5 py-3 font-medium">{t("certs.colStatus")}</th>
                </tr>
              </thead>
              <tbody>
                {hosts === null && (
                  <tr>
                    <td colSpan={5} className="px-5 py-4">
                      <span className="inline-flex items-center gap-2 text-slate-400">
                        <Spinner size={13} />
                        {t("common.loading")}
                      </span>
                    </td>
                  </tr>
                )}
                {hosts?.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-5 py-8 text-center text-slate-400">
                      {t("drift.noHosts")}
                    </td>
                  </tr>
                )}
                {hosts !== null && hosts.length > 0 && rows.length === 0 && pendingHosts.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-5 py-8 text-center text-slate-400">
                      {t("certs.noneFound")}
                    </td>
                  </tr>
                )}
                {rows.map(({ host, cert }) => (
                  <tr key={`${host.id}-${cert.uuid}`} className="border-b border-slate-100 last:border-0">
                    <td className="px-5 py-3 text-slate-500">{host.name}</td>
                    <td className="px-5 py-3 font-medium text-slate-900">{cert.common_name ?? cert.descr}</td>
                    <td className="px-5 py-3 text-slate-600">{cert.issuer}</td>
                    <td className="px-5 py-3 text-slate-600">
                      {cert.valid_to ? new Date(cert.valid_to).toLocaleDateString(locale) : <span className="text-slate-300">—</span>}
                    </td>
                    <td className="px-5 py-3">
                      <CertStatusBadge status={cert.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          {erroredHosts.length > 0 && (
            <div className="mt-3 space-y-1">
              {erroredHosts.map((host) => (
                <p key={host.id} className="text-sm text-red-500" title={byHost[host.id]?.error ?? undefined}>
                  {t("certs.hostUnavailable", { host: host.name })}
                </p>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "trust" && (
        <div>
          <p className="mb-4 text-sm text-slate-500">
            {t("certs.trustIntro")}
          </p>
          {managedError && <p role="alert" className="mb-4 text-sm text-red-600">{managedError}</p>}
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <Button onClick={() => setManagedForm({ editing: null })} disabled={readOnly || !hosts || hosts.length === 0}>
              <Plus size={16} /> {t("certs.newCert")}
            </Button>
            {(!hosts || hosts.length === 0) && <p className="mt-2 text-sm text-slate-500">{t("certs.createHostFirst")}</p>}
            {certFilter && <Button variant="ghost" onClick={() => router.push("/certificates")}>{t("certs.showAll")}</Button>}
          </div>
          <fieldset disabled={readOnly} className="contents">
          <div className="space-y-3">
            {managedCerts === null && <Card className="p-6"><Loading>{t("common.loading")}</Loading></Card>}
            {managedCerts?.length === 0 && <EmptyState icon={ShieldCheck} title={t("certs.noManaged")} />}
            {(certFilter ? managedCerts?.filter((c) => c.id === certFilter) : managedCerts)?.map((cert) => (
              <Card key={cert.id} className="p-5">
                <div className="flex flex-col items-start justify-between gap-3 sm:flex-row">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <strong className="text-slate-900">{cert.description}</strong>
                      <DeployStatusBadge status={cert.status} />
                    </div>
                    <p className="mt-1 text-sm text-slate-500">
                      {t("certs.actionAndCn", { host: hostName(cert.host_id), action: cert.action })} {cert.commonname && `· CN: ${cert.commonname}`}
                    </p>
                    {cert.message && <p className="mt-1 text-sm text-red-700">{ts(cert.message)}</p>}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button variant="ghost" disabled={managedBusy === cert.id} onClick={() => setManagedForm({ editing: cert })}>
                      <Pencil size={15} /> {t("certs.edit")}
                    </Button>
                    <Button variant="primary" disabled={managedBusy === cert.id} onClick={() => pushManagedCert(cert)}>
                      <Rocket size={15} /> {managedBusy === cert.id ? "…" : t("certs.push")}
                    </Button>
                    <Button variant="dangerSubtle" disabled={managedBusy === cert.id} onClick={() => deleteManagedCert(cert)}>
                      <Trash2 size={15} /> {t("certs.delete")}
                    </Button>
                  </div>
                </div>
              </Card>
            ))}
          </div>
          {managedForm && hosts && (
            <ManagedCertificateForm
              hosts={hosts}
              editing={managedForm.editing}
              onClose={() => setManagedForm(null)}
              onSaved={() => {
                toast.show(managedForm.editing ? t("certs.certSaved") : t("certs.certCreated"), "success");
                setManagedForm(null);
                loadManaged();
              }}
            />
          )}
          </fieldset>
        </div>
      )}

      <fieldset disabled={readOnly} className="contents">
      {tab === "acme" && (
        <div className="space-y-8">
          <p className="text-sm text-slate-500">
            {t("certs.acmeIntro")}
          </p>
          {acmeError && <p role="alert" className="text-sm text-red-600">{acmeError}</p>}

          <div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-900">{t("certs.accounts")}</h2>
              <Button onClick={() => setAccountForm({ editing: null })} disabled={!hosts || hosts.length === 0}>
                <Plus size={16} /> {t("certs.newAccount")}
              </Button>
            </div>
            <div className="space-y-3">
              {accounts === null && <Card className="p-6"><Loading>{t("common.loading")}</Loading></Card>}
              {accounts?.length === 0 && <EmptyState icon={ShieldCheck} title={t("certs.noAccounts")} />}
              {accounts?.map((account) => (
                <Card key={account.id} className="p-5">
                  <div className="flex flex-col items-start justify-between gap-3 sm:flex-row">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <strong className="text-slate-900">{account.name}</strong>
                        <DeployStatusBadge status={account.status} />
                        {account.register_status && (
                          <Badge tone={account.register_status === "ok" ? "success" : "danger"}>
                            {t("certs.registration", { state: account.register_status === "ok" ? t("certs.registrationOk") : t("certs.registrationFailed") })}
                          </Badge>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-slate-500">
                        {hostName(account.host_id)} · CA: {account.ca}
                      </p>
                      {account.message && <p className="mt-1 text-sm text-red-700">{ts(account.message)}</p>}
                      {account.register_message && <p className="mt-1 break-words text-xs text-slate-500">{ts(account.register_message)}</p>}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button variant="ghost" disabled={acmeBusy === account.id} onClick={() => setAccountForm({ editing: account })}>
                        <Pencil size={15} /> {t("certs.edit")}
                      </Button>
                      <Button variant="primary" disabled={acmeBusy === account.id} onClick={() => pushAccount(account)}>
                        <Rocket size={15} /> {acmeBusy === account.id ? "…" : t("certs.push")}
                      </Button>
                      <Button
                        variant="danger"
                        disabled={acmeBusy === account.id || !account.opnsense_uuid}
                        title={!account.opnsense_uuid ? t("certs.pushFirst") : undefined}
                        onClick={() => setRegisterTarget(account)}
                      >
                        <ShieldCheck size={15} /> {t("certs.register")}
                      </Button>
                      <Button variant="dangerSubtle" disabled={acmeBusy === account.id} onClick={() => deleteAccount(account)}>
                        <Trash2 size={15} /> {t("certs.delete")}
                      </Button>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-900">{t("certs.requests")}</h2>
              <Button onClick={() => setRequestForm({ editing: null })} disabled={!hosts || !accounts || accounts.length === 0}>
                <Plus size={16} /> {t("certs.newRequest")}
              </Button>
            </div>
            {accounts?.length === 0 && <p className="mb-3 text-sm text-slate-500">{t("certs.createAccountFirst")}</p>}
            <div className="space-y-3">
              {requests === null && <Card className="p-6"><Loading>{t("common.loading")}</Loading></Card>}
              {requests?.length === 0 && <EmptyState icon={ShieldCheck} title={t("certs.noRequests")} />}
              {requests?.map((req) => (
                <Card key={req.id} className="p-5">
                  <div className="flex flex-col items-start justify-between gap-3 sm:flex-row">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <strong className="text-slate-900">{req.name}</strong>
                        <DeployStatusBadge status={req.status} />
                        {req.sign_status && (
                          <Badge tone={req.sign_status === "triggered" ? "info" : "danger"}>
                            {t("certs.issuance", { state: req.sign_status === "triggered" ? t("certs.issuanceTriggered") : t("certs.issuanceError") })}
                          </Badge>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-slate-500">
                        {t("certs.requestMeta", { host: hostName(req.host_id), account: accountName(req.account_id), days: req.renew_interval })}
                      </p>
                      {req.remote_status_code !== null && (
                        <p className="mt-1 text-xs text-slate-500">
                          {t("certs.firewallStatusCode", { code: req.remote_status_code })}
                          {req.remote_last_update && t("certs.lastIssuance", { time: new Date(req.remote_last_update).toLocaleString(locale) })}
                        </p>
                      )}
                      {req.message && <p className="mt-1 text-sm text-red-700">{ts(req.message)}</p>}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button variant="ghost" disabled={acmeBusy === req.id} onClick={() => setRequestForm({ editing: req })}>
                        <Pencil size={15} /> {t("certs.edit")}
                      </Button>
                      <Button variant="primary" disabled={acmeBusy === req.id} onClick={() => pushRequest(req)}>
                        <Rocket size={15} /> {acmeBusy === req.id ? "…" : t("certs.push")}
                      </Button>
                      <Button
                        variant="danger"
                        disabled={acmeBusy === req.id || !req.opnsense_uuid}
                        title={!req.opnsense_uuid ? t("certs.pushFirst") : undefined}
                        onClick={() => setSignTarget(req)}
                      >
                        <ShieldCheck size={15} /> {t("certs.issueNow")}
                      </Button>
                      <Button variant="ghost" disabled={acmeBusy === req.id || !req.opnsense_uuid} onClick={() => pollRequest(req)}>
                        <RefreshCw size={15} /> {t("certs.status")}
                      </Button>
                      <Button variant="dangerSubtle" disabled={acmeBusy === req.id} onClick={() => deleteRequest(req)}>
                        <Trash2 size={15} /> {t("certs.delete")}
                      </Button>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          </div>

          {accountForm && hosts && (
            <AcmeAccountForm
              hosts={hosts}
              editing={accountForm.editing}
              onClose={() => setAccountForm(null)}
              onSaved={() => {
                toast.show(accountForm.editing ? t("certs.accountSaved") : t("certs.accountCreated"), "success");
                setAccountForm(null);
                loadAcme();
              }}
            />
          )}
          {requestForm && hosts && accounts && (
            <AcmeCertificateForm
              hosts={hosts}
              accounts={accounts}
              editing={requestForm.editing}
              onClose={() => setRequestForm(null)}
              onSaved={() => {
                toast.show(requestForm.editing ? t("certs.requestSaved") : t("certs.requestCreated"), "success");
                setRequestForm(null);
                loadAcme();
              }}
            />
          )}
          {registerTarget && (
            <AcmeActionConfirm
              title={t("certs.registerTitle", { name: registerTarget.name })}
              actionLabel={t("certs.register")}
              busyLabel={t("certs.registering")}
              onClose={() => setRegisterTarget(null)}
              onConfirm={async () => {
                await api.registerAcmeAccount(registerTarget.id);
                toast.show(t("certs.accountRegistered", { name: registerTarget.name }), "success");
                await loadAcme();
              }}
            />
          )}
          {signTarget && (
            <AcmeActionConfirm
              title={t("certs.issueTitle", { name: signTarget.name })}
              actionLabel={t("certs.issueNow")}
              busyLabel={t("certs.triggering")}
              onClose={() => setSignTarget(null)}
              onConfirm={async () => {
                await api.signAcmeCertificateRequest(signTarget.id);
                toast.show(t("certs.issuanceTriggeredToast", { name: signTarget.name }), "success");
                await loadAcme();
              }}
            />
          )}
        </div>
      )}
      </fieldset>
    </div>
  );
}
