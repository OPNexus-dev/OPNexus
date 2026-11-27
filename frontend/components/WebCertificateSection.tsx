"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useConfirm } from "@/components/ui/Confirm";
import { FieldGroup } from "@/components/ui/Field";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { InfoTip } from "@/components/ui/Tooltip";
import { api, WebCertInfo, WebCertState } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

const POLL_MS = 2000;
const GIVE_UP_MS = 150_000;   // the proxy is recreated and the page's own connection breaks for a moment
const textareaClass = "w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";

function Warning({ children }: { children: React.ReactNode }) {
  return (
    <div role="status" className="flex items-start gap-2 rounded-lg border border-[rgb(var(--tone-warning-border))] bg-[rgb(var(--tone-warning-bg))] p-3 text-sm text-[rgb(var(--tone-warning-text))]">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden />
      <div>{children}</div>
    </div>
  );
}

/** Dialog: certificate and key in, check, then install. The button stays grey until exactly what is shown has been checked. */
function InstallDialog({ onClose, onStarted }: { onClose: () => void; onStarted: (verb: "install", id: string) => void }) {
  const { t, errorMessage, locale } = useLanguage();
  const [cert, setCert] = useState("");
  const [key, setKey] = useState("");
  const [checked, setChecked] = useState<{ info: WebCertInfo; for: string } | null>(null);
  const [busy, setBusy] = useState<"check" | "install" | null>(null);
  const [error, setError] = useState("");
  const current = `${cert}\u0000${key}`;
  const verified = checked !== null && checked.for === current ? checked.info : null;
  const filled = cert.trim() !== "" && key.trim() !== "";

  async function readFile(file: File | undefined, set: (value: string) => void) {
    if (!file) return;
    try {
      set(await file.text());
      setError("");
    } catch {
      setError(t("common.requestFailed"));
    }
  }

  async function check() {
    setBusy("check"); setError("");
    try {
      const { certificate } = await api.webCertCheck(cert, key);
      setChecked({ info: certificate, for: current });
    } catch (e) {
      setChecked(null);
      setError(errorMessage(e, "common.requestFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function install() {
    setBusy("install"); setError("");
    try {
      const { job } = await api.webCertInstall(cert, key);
      onStarted("install", job.id);   // closes the dialog: the key leaves this page's memory with it
    } catch (e) {
      setError(errorMessage(e, "common.requestFailed"));
      setBusy(null);
    }
  }

  return (
    <Modal title={t("webCert.dialogTitle")} onClose={onClose} wide>
      <div className="space-y-4">
        <FieldGroup label={t("webCert.certificate")}>
          <textarea className={textareaClass} rows={6} value={cert} onChange={(e) => setCert(e.target.value)} placeholder="-----BEGIN CERTIFICATE-----" aria-label={t("webCert.certificate")} spellCheck={false} autoComplete="off" />
          <input type="file" accept=".pem,.crt,.cer,text/plain" aria-label={`${t("webCert.certificate")}: ${t("webCert.chooseFile")}`} className="mt-1 block text-xs text-slate-600"
                 onChange={(e) => void readFile(e.target.files?.[0], setCert)} />
        </FieldGroup>
        <FieldGroup label={t("webCert.key")}>
          <textarea className={textareaClass} rows={4} value={key} onChange={(e) => setKey(e.target.value)} placeholder="-----BEGIN PRIVATE KEY-----" aria-label={t("webCert.key")} spellCheck={false} autoComplete="off" />
          <input type="file" accept=".pem,.key,text/plain" aria-label={`${t("webCert.key")}: ${t("webCert.chooseFile")}`} className="mt-1 block text-xs text-slate-600"
                 onChange={(e) => void readFile(e.target.files?.[0], setKey)} />
        </FieldGroup>
        <p className="text-xs text-slate-500">{t("webCert.keyNote")}</p>

        {verified && (
          <div className="space-y-2 rounded-lg border border-slate-200 p-3 text-sm">
            <p className="flex items-center gap-2 font-medium text-[rgb(var(--tone-success-text))]"><CheckCircle2 size={16} aria-hidden />{t("webCert.checkOk")}</p>
            <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
              <dt className="text-slate-500">{t("webCert.subject")}</dt><dd className="break-all">{verified.subject}</dd>
              <dt className="text-slate-500">{t("webCert.validUntil")}</dt><dd>{formatEnd(verified, locale, t)}</dd>
              <dt className="text-slate-500">{t("webCert.addresses")}</dt><dd className="flex flex-wrap gap-1">{verified.names.map((n) => <Badge key={n} tone="neutral">{n}</Badge>)}</dd>
            </dl>
            {verified.uncovered.length > 0 && <Warning>{t("webCert.checkNotCovered", { names: verified.uncovered.join(", ") })}</Warning>}
          </div>
        )}
        <p className="text-xs text-slate-500">{t("webCert.restartNote")}</p>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button variant="outline" onClick={() => void check()} disabled={!filled || verified !== null || busy !== null} loading={busy === "check"}>{t("webCert.check")}</Button>
          <Button onClick={() => void install()} disabled={verified === null || busy !== null} loading={busy === "install"}>{t("webCert.installNow")}</Button>
        </div>
      </div>
    </Modal>
  );
}

function formatEnd(info: Pick<WebCertInfo, "not_after" | "days_left">, locale: string, t: (key: string, params?: Record<string, string | number>) => string): string {
  const parsed = new Date(info.not_after);
  const when = Number.isNaN(parsed.getTime()) ? info.not_after : parsed.toLocaleDateString(locale);
  if (info.days_left === null) return when;
  return info.days_left < 0 ? `${when} (${t("webCert.expired")})` : `${when} (${t("webCert.daysLeft", { n: info.days_left })})`;
}

/** The certificate the proxy serves for OPNexus, and replacing it by an own one. Admin only: renders nothing for other roles or without the updater. */
export function WebCertificateSection() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const confirmAction = useConfirm();
  const [state, setState] = useState<WebCertState | null>(null);
  const [hidden, setHidden] = useState(false);
  const [dialog, setDialog] = useState(false);
  const [watching, setWatching] = useState<{ id: string; verb: "install" | "remove"; since: number } | null>(null);
  const [result, setResult] = useState<{ ok: boolean; verb: "install" | "remove"; log: string[] } | null>(null);
  const [lost, setLost] = useState(false);
  const wasRunning = useRef<{ id: string; verb: "install" | "remove" } | null>(null);

  const load = useCallback(async () => {
    const next = await api.webCert();
    setState(next);
    return next;
  }, []);

  useEffect(() => { load().catch(() => setHidden(true)); }, [load]);

  const running = state?.job?.state === "running";
  useEffect(() => {
    if (state?.job?.state === "running") wasRunning.current = { id: state.job.id, verb: state.job.verb };
  }, [state]);

  // While a change runs the proxy is recreated: requests may fail for a moment, which is expected and ignored.
  useEffect(() => {
    if (!running && !watching) return;
    const since = watching?.since ?? Date.now();
    const timer = setInterval(async () => {
      try {
        const next = await load();
        const job = next.job;
        if (job && job.state !== "running" && wasRunning.current?.id === job.id) {
          const verb = wasRunning.current.verb;
          wasRunning.current = null;
          setWatching(null);
          setResult({ ok: job.state === "succeeded", verb, log: next.log ?? [] });
          if (job.state === "succeeded") toast.show(t(verb === "install" ? "webCert.doneInstall" : "webCert.doneRemove"), "success");
        }
      } catch {
        if (Date.now() - since > GIVE_UP_MS) {
          wasRunning.current = null;
          setWatching(null);
          setLost(true);
          setState((prev) => (prev ? { ...prev, job: null } : prev));   // stop polling and show the hint instead of a spinner forever
        }
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [running, watching, load, toast, t]);

  if (hidden) return null;

  const status = state?.status ?? null;
  const standby = state?.ha_role === "standby";
  const busy = running || watching !== null;
  const own = status?.source === "custom";

  function started(verb: "install" | "remove", id: string) {
    setDialog(false); setResult(null); setLost(false);
    wasRunning.current = { id, verb };
    setWatching({ id, verb, since: Date.now() });
    setState((prev) => ({ ...(prev ?? { available: true }), status: null, error: null, job: { id, verb, state: "running", started_at: new Date().toISOString(), finished_at: null, exit_code: null } }));
  }

  async function remove() {
    if (!(await confirmAction(t("webCert.removeConfirm"), { danger: true }))) return;
    try {
      const { job } = await api.webCertRemove();
      started("remove", job.id);
    } catch (e) {
      toast.show(errorMessage(e, "common.requestFailed"), "danger");
    }
  }

  return (
    <section id="web-certificate" className="scroll-mt-4 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("webCert.title")}
        <InfoTip text={t("webCert.intro")} />
        {status && <Badge tone={own ? "info" : "neutral"}>{own ? t("webCert.badgeOwn") : t("webCert.badgeInternal")}</Badge>}
      </h2>

      {!state && <p className="text-sm text-slate-500">{t("webCert.loading")}</p>}
      {state && !state.available && <p className="text-sm text-slate-600">{t("webCert.unavailable")}</p>}
      {state?.available && state.error && !busy && <p role="alert" className="text-sm text-red-600">{t("webCert.statusError", { reason: state.error })}</p>}

      {status && (
        <>
          <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[10rem_1fr]">
            <dt className="text-slate-500">{t("webCert.source")}</dt>
            <dd>{status.source === "custom" ? t("webCert.sourceCustom") : status.source === "all" ? t("webCert.sourceAll") : t("webCert.sourcePerAddress")}</dd>
            <dt className="text-slate-500">{t("webCert.addresses")}</dt>
            <dd className="flex flex-wrap gap-1">{status.names.map((n) => <Badge key={n} tone="neutral">{n}</Badge>)}</dd>
            <dt className="text-slate-500">{t("webCert.subject")}</dt><dd className="break-all">{status.subject}</dd>
            <dt className="text-slate-500">{t("webCert.issuer")}</dt><dd className="break-all">{status.issuer}</dd>
            <dt className="text-slate-500">{t("webCert.validUntil")}</dt>
            <dd className="flex flex-wrap items-center gap-2">
              {formatEnd(status, locale, t)}
              {status.days_left !== null && status.days_left < 30 && <Badge tone={status.days_left < 0 ? "danger" : "warning"}>{status.days_left < 0 ? t("webCert.expired") : t("webCert.daysLeft", { n: status.days_left })}</Badge>}
            </dd>
            <dt className="text-slate-500">{t("webCert.fingerprint")}</dt><dd className="break-all font-mono text-xs">{status.fingerprint}</dd>
          </dl>
          {status.custom_not_served && <Warning>{t("webCert.notServed")}</Warning>}
          {status.uncovered.length > 0 && <Warning>{t("webCert.uncovered", { names: status.uncovered.join(", ") })}</Warning>}
          {own && <p className="text-xs text-slate-500">{t("webCert.noRenew")}</p>}
        </>
      )}

      {running && (
        <div role="status" className="flex items-center gap-2 rounded-lg border border-slate-200 p-3 text-sm">
          <Loader2 size={16} className="animate-spin" aria-hidden />{t("webCert.running")}
        </div>
      )}
      {lost && !running && <Warning>{t("webCert.lostConnection")}</Warning>}
      {result && (
        <div role="status" className="space-y-2 rounded-lg border border-slate-200 p-3 text-sm">
          <p className={result.ok ? "flex items-center gap-2 font-medium text-[rgb(var(--tone-success-text))]" : "flex items-center gap-2 font-medium text-red-600"}>
            {result.ok ? <CheckCircle2 size={16} aria-hidden /> : <AlertTriangle size={16} aria-hidden />}
            {result.ok ? t(result.verb === "install" ? "webCert.doneInstall" : "webCert.doneRemove") : t("webCert.failed")}
          </p>
          {!result.ok && result.log.length > 0 && (
            <details><summary className="cursor-pointer text-slate-600">{t("webCert.showLog")}</summary>
              <pre className="mt-1 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">{result.log.join("\n")}</pre>
            </details>
          )}
        </div>
      )}

      {state?.available && (
        <>
          {state.ha_role === "primary" && <p className="text-xs text-slate-500">{t("webCert.haPrimary")}</p>}
          {standby && <p className="text-xs text-slate-500">{t("webCert.haStandby")}</p>}
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => setDialog(true)} disabled={busy || standby}>{t("webCert.install")}</Button>
            {own && <Button variant="outline" onClick={() => void remove()} disabled={busy || standby}>{t("webCert.remove")}</Button>}
          </div>
        </>
      )}
      {dialog && <InstallDialog onClose={() => setDialog(false)} onStarted={started} />}
    </section>
  );
}
