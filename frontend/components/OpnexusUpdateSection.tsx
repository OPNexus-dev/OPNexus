"use client";

import { Badge, badgeToneClasses } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { InfoTip } from "@/components/ui/Tooltip";
import { api, UpdaterStatus } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useNotAdmin } from "@/lib/useReadOnly";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";

const POLL_MS = 2000;
const RELOAD_DELAY_MS = 3500;

/**
 * Settings card "OPNexus aktualisieren": zeigt die verfügbare Version und löst
 * das Update über den optionalen Updater-Container aus (siehe updater/server.py).
 * Das Backend selbst fasst den Host nie an. Während des Updates starten Backend
 * und Proxy neu — die Karte fragt weiter nach und übersteht die Lücke.
 */
export function OpnexusUpdateSection() {
  const { t, errorMessage, locale } = useLanguage();
  const readOnly = useNotAdmin();
  const [status, setStatus] = useState<UpdaterStatus | null>(null);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState<"update" | "rollback" | null>(null);
  const [busy, setBusy] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  // Wurde in dieser Sitzung ein laufender Auftrag gesehen? Nur dann ist ein
  // "succeeded" Anlass, die Seite neu zu laden (neue Frontend-Version).
  const sawRunning = useRef(false);
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.updaterStatus();
      setStatus(next);
      setReconnecting(false);
      setError("");
      if (next.job?.state === "running") sawRunning.current = true;
      return next;
    } catch (e) {
      if (sawRunning.current) {
        setReconnecting(true); // Dienste starten gerade neu — weiter versuchen
      } else {
        setError(errorMessage(e, "common.requestFailed"));
      }
      return null;
    }
  }, [errorMessage]);

  useEffect(() => {
    if (readOnly) return;
    void load();
  }, [load, readOnly]);

  const running = status?.job?.state === "running" || reconnecting;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [running, load]);

  const job = status?.job ?? null;
  const finishedNow = !!job && job.state === "succeeded" && sawRunning.current;
  useEffect(() => {
    if (!finishedNow) return;
    reloadTimer.current = setTimeout(() => window.location.reload(), RELOAD_DELAY_MS);
    return () => {
      if (reloadTimer.current) clearTimeout(reloadTimer.current);
    };
  }, [finishedNow]);

  if (readOnly) return null;

  async function start(kind: "update" | "rollback") {
    setBusy(true);
    setError("");
    try {
      if (kind === "update" && status?.latest) await api.updaterUpdate(status.latest);
      else if (kind === "rollback") await api.updaterRollback();
      sawRunning.current = true;
      setConfirm(null);
      await load();
    } catch (e) {
      setConfirm(null);
      setError(errorMessage(e, "settings.update.startFailed"));
    } finally {
      setBusy(false);
    }
  }

  const phases = status?.phases ?? [];
  const steps = phases.filter((p, i) => p !== phases[i - 1]);
  const isRollbackJob = job?.action === "rollback";
  const finishedAt = job?.finished_at ? new Date(job.finished_at).toLocaleString(locale) : "";
  const releaseUrl = status?.release_url && /^https?:\/\//.test(status.release_url) ? status.release_url : null;

  return (
    <section id="opnexus-update" className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("settings.update.title")}
        <InfoTip text={t("settings.update.hint")} />
      </h2>

      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {!status && !error && <p className="text-sm text-slate-500">{t("common.loading")}</p>}

      {status && (
        <>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <dt>{t("settings.update.installed")}</dt>
            <dd>{status.current}</dd>
            <dt>{t("settings.update.latest")}</dt>
            <dd className="flex flex-wrap items-center gap-2">
              {status.latest ?? "—"}
              {status.update_available && <Badge tone="info">{t("settings.update.badge")}</Badge>}
              {releaseUrl && (
                <a href={releaseUrl} target="_blank" rel="noopener noreferrer" className="font-medium text-brand-600 underline">
                  {t("settings.update.notes")}
                </a>
              )}
            </dd>
          </dl>

          {!status.feed_enabled && <p className="text-sm text-slate-600">{t("settings.update.feedOff")}</p>}
          {status.feed_enabled && status.feed_error && <p className="text-sm text-amber-700">{t("settings.update.feedError")}</p>}
          {status.feed_enabled && !status.feed_error && !status.update_available && (
            <p className="flex items-center gap-2 text-sm text-emerald-700">
              <CheckCircle2 size={16} aria-hidden="true" /> {t("settings.update.upToDate")}
              {status.checked_at && <span className="text-slate-500">· {t("settings.update.checked", { time: new Date(status.checked_at).toLocaleString(locale) })}</span>}
            </p>
          )}

          {!status.available && (
            <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
              {status.reason === "not_enabled" || status.reason === "unauthorized" ? (
                <>
                  <p className="font-medium">{t("settings.update.notEnabledTitle")}</p>
                  <p>{t("settings.update.notEnabledBody")}</p>
                  <p>{t("settings.update.notEnabledStep1")}</p>
                  <pre className="overflow-x-auto rounded bg-white p-2 text-xs">COMPOSE_PROFILES=updater</pre>
                  <p>{t("settings.update.notEnabledStep2")}</p>
                  <pre className="overflow-x-auto rounded bg-white p-2 text-xs">docker compose up -d</pre>
                  <p className="text-slate-500">{t("settings.update.notEnabledCli")}</p>
                </>
              ) : (
                <p>{t("settings.update.unreachable")}</p>
              )}
            </div>
          )}

          {status.available && status.mode === "local" && (
            <p className="text-sm text-slate-600">{t("settings.update.localMode")}</p>
          )}

          {status.available && status.major_change && status.latest && (
            <p className={clsx("flex items-start gap-2 rounded-lg border p-3 text-sm", badgeToneClasses.warning)}>
              <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
              {t("settings.update.majorChange", { latest: status.latest })}
            </p>
          )}

          {running && (
            <div role="status" className={clsx("space-y-2 rounded-lg border p-3 text-sm", badgeToneClasses.info)}>
              <p className="flex items-center gap-2 font-medium">
                <Loader2 size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {isRollbackJob ? t("settings.update.rollingBack") : t("settings.update.running", { target: job?.target ?? status.latest ?? "" })}
              </p>
              {steps.length > 0 && (
                <ol className="list-inside list-decimal text-xs">
                  {steps.map((step) => (
                    <li key={step}>{t(`settings.update.phase.${step}`)}</li>
                  ))}
                </ol>
              )}
              {reconnecting && <p className="text-xs">{t("settings.update.reconnecting")}</p>}
            </div>
          )}

          {!running && job && job.state === "succeeded" && sawRunning.current && (
            <p role="status" className="flex items-center gap-2 text-sm text-emerald-700">
              <CheckCircle2 size={16} aria-hidden="true" />
              {isRollbackJob
                ? t("settings.update.result.succeededRollback", { version: status.current })
                : t("settings.update.result.succeededUpdate", { version: status.current })}
            </p>
          )}
          {!running && job?.state === "rolled_back" && (
            <p role="alert" className={clsx("rounded-lg border p-3 text-sm", badgeToneClasses.warning)}>
              {t("settings.update.result.rolledBack", { target: job.target ?? "" })}
              {finishedAt && <span className="mt-1 block text-xs opacity-80">{t("settings.update.resultTime", { time: finishedAt })}</span>}
            </p>
          )}
          {!running && job?.state === "failed" && (
            <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">
              {t("settings.update.result.failed")}
              {finishedAt && <span className="mt-1 block text-xs opacity-80">{t("settings.update.resultTime", { time: finishedAt })}</span>}
            </p>
          )}

          {status.log.length > 0 && (job?.state !== "succeeded" || running) && (
            <details className="text-xs" open={!running && job?.state !== "succeeded"}>
              <summary className="cursor-pointer text-slate-600">{t("settings.update.showLog")}</summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3">{status.log.join("\n")}</pre>
            </details>
          )}

          {status.can_update && status.latest && !running && (
            <Button onClick={() => setConfirm("update")} disabled={busy}>
              {t("settings.update.button", { latest: status.latest })}
            </Button>
          )}

          {status.available && status.mode === "release" && status.rollback_available && !running && (
            <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-3 text-sm">
              <Button variant="outline" onClick={() => setConfirm("rollback")} disabled={busy}>
                {t("settings.update.rollback.button")}
              </Button>
              <span className="text-slate-500">{t("settings.update.rollback.hint", { version: status.rollback_version ?? "—" })}</span>
            </div>
          )}
        </>
      )}

      {confirm === "update" && status?.latest && (
        <Modal title={t("settings.update.confirmTitle", { latest: status.latest })} onClose={() => setConfirm(null)}>
          <div className="space-y-3 text-sm text-slate-700">
            <p>{t("settings.update.confirmBody")}</p>
            <p>{t("settings.update.confirmSafety")}</p>
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setConfirm(null)} disabled={busy}>{t("common.cancel")}</Button>
              <Button onClick={() => void start("update")} loading={busy}>{t("settings.update.confirm")}</Button>
            </div>
          </div>
        </Modal>
      )}

      {confirm === "rollback" && status && (
        <Modal title={t("settings.update.rollback.confirmTitle", { version: status.rollback_version ?? "—" })} onClose={() => setConfirm(null)}>
          <div className="space-y-3 text-sm text-slate-700">
            <p className={status.rollback_loses_data ? clsx("rounded-lg border p-3", badgeToneClasses.warning) : ""}>
              {status.rollback_loses_data ? t("settings.update.rollback.dataLoss") : t("settings.update.rollback.noDataLoss")}
            </p>
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setConfirm(null)} disabled={busy}>{t("common.cancel")}</Button>
              <Button variant={status.rollback_loses_data ? "danger" : "primary"} onClick={() => void start("rollback")} loading={busy}>
                {t("settings.update.rollback.confirm")}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </section>
  );
}
