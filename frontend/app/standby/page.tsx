"use client";

// The standby's own door (docs/DESIGN-HA-UI.md section 4). The database of this node is a read-only replica, so the normal sign-in
// cannot work; this page signs an administrator in through a separate, write-free door, shows the state of the pair and offers
// the takeover for when the primary is down. Everything else on this node answers NODE_STANDBY and lands here.
import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { FieldGroup, Input } from "@/components/ui/Field";
import { api, ApiError, StandbyJob, StandbyStatus } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import clsx from "clsx";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type Phase = "checking" | "login" | "status" | "running" | "done" | "failed";

function ageText(seconds: number | null | undefined, t: (key: string, params?: Record<string, string | number>) => string): string {
  if (seconds == null) return "—";
  if (seconds >= 3600) return t("standby.age.hours", { h: Math.floor(seconds / 3600), m: Math.floor((seconds % 3600) / 60) });
  if (seconds >= 120) return t("standby.age.minutes", { m: Math.floor(seconds / 60) });
  return t("standby.age.seconds", { s: seconds });
}

export default function StandbyPage() {
  const { t, errorMessage } = useLanguage();
  const [phase, setPhase] = useState<Phase>("checking");
  const [status, setStatus] = useState<StandbyStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needCode, setNeedCode] = useState(false);
  const [armed, setArmed] = useState(false);
  const [sentence, setSentence] = useState("");
  const [log, setLog] = useState<string[]>([]);
  const [job, setJob] = useState<StandbyJob | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopTimer = () => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
  };

  const loadStatus = useCallback(async () => {
    try {
      const next = await api.standbyStatus();
      setStatus(next);
      setError(null);
      if (next.job?.state === "running") setPhase("running");
      else setPhase((p) => (p === "checking" || p === "login" ? "status" : p));
    } catch (err) {
      if (err instanceof ApiError && err.code === "NOT_STANDBY") { window.location.replace("/"); return; }
      if (err instanceof ApiError && err.status === 401) { setPhase("login"); return; }
      setError(errorMessage(err));
    }
  }, [errorMessage]);

  // First look: signed in already (cookie)? A node that is no standby any more (it took over, or never was one) belongs on the normal pages.
  useEffect(() => {
    api.standbySession()
      .then(() => loadStatus())
      .catch((err) => {
        if (err instanceof ApiError && err.code === "NOT_STANDBY") window.location.replace("/");
        else setPhase("login");
      });
    return stopTimer;
  }, [loadStatus]);

  // The state refreshes while the page is open (the partner may come back, the replication may catch up).
  useEffect(() => {
    if (phase !== "status") return;
    const id = setInterval(() => void loadStatus(), 10000);
    return () => clearInterval(id);
  }, [phase, loadStatus]);

  // While the takeover runs: follow the job. The backend of this node restarts on the way (it becomes the primary), and with it the key
  // of the sign-in cookie changes: the job query then answers 401 or NOT_STANDBY, which means "probably done", not "signed out". The
  // public health answer decides: done = this node reports itself as leader.
  useEffect(() => {
    if (phase !== "running") return;
    let misses = 0;
    timer.current = setInterval(async () => {
      try {
        const next = await api.standbyJob();
        misses = 0;
        setLog(next.log);
        setJob(next.job);
        if (next.job?.state === "failed") { stopTimer(); setPhase("failed"); }
        return;
      } catch {
        // fall through: ask the health endpoint what became of this node
      }
      try {
        const health = await api.health();
        if (health.role === "leader") { stopTimer(); setPhase("done"); return; }
        misses = 0;   // answers, but is not leader yet (still restarting)
      } catch {
        misses += 1;   // proxy or backend restarting
      }
      if (misses > 90) { stopTimer(); setError(t("standby.lost")); setPhase("failed"); }
    }, 2000);
    return stopTimer;
  }, [phase, t]);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.standbyLogin({ username, password, ...(needCode ? { totp_code: code } : {}) });
      setPassword("");
      setCode("");
      setNeedCode(false);
      await loadStatus();
    } catch (err) {
      if (err instanceof ApiError && err.code === "TOTP_REQUIRED") { setNeedCode(true); setError(null); }
      else if (err instanceof ApiError && err.code === "NOT_STANDBY") window.location.replace("/login");
      else setError(errorMessage(err, "login.failed"));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    await api.standbyLogout().catch(() => undefined);
    setStatus(null);
    setPhase("login");
  }

  async function takeOver() {
    if (!status) return;
    setBusy(true);
    setError(null);
    try {
      const force = status.promote.mode === "force";
      const started = await api.standbyPromote(force ? { force: true, confirm: sentence } : {});
      setJob(started.job);
      setLog([]);
      setArmed(false);
      setPhase("running");
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError && err.status === 409) void loadStatus();
    } finally {
      setBusy(false);
    }
  }

  const mode = status?.promote.mode;
  const missing = status && !status.secrets.complete;
  const sentenceOk = status != null && sentence.trim() === status.promote.force_sentence;

  const rows: [string, React.ReactNode][] = status ? [
    [t("standby.rows.node"), <span key="n">{status.node} <Badge tone="info">{t("standby.badge.standby")}</Badge></span>],
    [t("standby.rows.partner"), status.partner.health
      ? t("standby.partnerSeen", { ip: status.peer_ip, role: status.partner.health.role, epoch: status.partner.health.epoch })
      : t("standby.partnerSilent", { ip: status.peer_ip })],
    [t("standby.rows.agent"), status.partner.agent == null ? t("standby.agent.unknown") : status.partner.agent ? t("standby.agent.up") : t("standby.agent.down")],
    [t("standby.rows.epoch"), String(status.epoch)],
    [t("standby.rows.replication"), status.replication.state
      ? (status.replication.lag_seconds != null
          ? t("standby.replicationValue", { state: status.replication.state, lag: `${status.replication.lag_seconds.toFixed(1)} s` })
          : status.replication.state)
      : t("standby.replicationNone")],
    [t("standby.rows.lastChange"), ageText(status.last_replay_age_seconds, t)],
    [t("standby.rows.sync"), ageText(status.sync_age_seconds, t)],
    [t("standby.rows.vip"), status.vip ?? "—"],
  ] : [];

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-slate-50 px-4 py-8">
      <div className="w-full max-w-xl space-y-4">
        <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-xl sm:p-8">
          <Logo className="mb-3 h-8 w-auto" />
          <h1 className="mb-1 text-lg font-medium">{t("standby.title")}</h1>
          <p className="mb-5 text-sm text-slate-500">{t("standby.intro")}</p>

          {phase === "checking" && <div className="flex items-center gap-2 text-sm text-slate-500" role="status"><Spinner size={14} /> {t("standby.checking")}</div>}

          {phase === "login" && (
            <form onSubmit={signIn} className="space-y-4">
              {!needCode ? (
                <>
                  <FieldGroup label={t("login.username")}>
                    <Input required autoFocus autoComplete="username" autoCapitalize="none" spellCheck={false} value={username} onChange={(e) => setUsername(e.target.value)} />
                  </FieldGroup>
                  <FieldGroup label={t("login.password")}>
                    <Input required type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
                  </FieldGroup>
                </>
              ) : (
                <div className="space-y-2">
                  <FieldGroup label={t("login.code")}>
                    <Input required autoFocus autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} inputMode="numeric" maxLength={8} value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" />
                  </FieldGroup>
                  <p className="text-xs text-slate-500">{t("standby.codeHint")}</p>
                  <button type="button" className="text-xs text-brand-600 hover:underline" onClick={() => { setNeedCode(false); setCode(""); setPassword(""); setError(null); }}>{t("login.back")}</button>
                </div>
              )}
              {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
              <Button type="submit" loading={busy} className="w-full justify-center">{busy ? t("login.signingIn") : t("login.signIn")}</Button>
              <p className="text-xs text-slate-500">{t("standby.adminOnly")}</p>
            </form>
          )}

          {(phase === "status" || phase === "running" || phase === "failed" || phase === "done") && status && (
            <div className="space-y-4">
              {phase !== "done" && <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[minmax(0,10rem)_1fr]">
                {rows.map(([label, value]) => (
                  <div key={label} className="contents">
                    <dt className="text-slate-600">{label}</dt>
                    <dd className="min-w-0 break-words">{value}</dd>
                  </div>
                ))}
              </dl>}

              {missing && phase !== "done" && (
                <p role="alert" className={clsx("flex items-start gap-2 rounded-lg border p-3 text-sm border-amber-300 bg-amber-50 text-amber-900")}>
                  <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                  <span>
                    {t("standby.secretsMissing")}
                    {status.secrets.hosts.length > 0 && <> {t("standby.secretsHosts", { names: status.secrets.hosts.join(", ") })}</>}
                    {status.secrets.totp.length > 0 && <> {t("standby.secretsTotp", { names: status.secrets.totp.join(", ") })}</>}
                  </span>
                </p>
              )}

              {phase === "status" && (
                <section aria-labelledby="takeover" className="space-y-3 border-t border-slate-200 pt-4">
                  <h2 id="takeover" className="text-sm font-medium">{t("standby.takeover.title")}</h2>
                  {mode === "unavailable" && <p role="alert" className="text-sm text-red-700">{t("standby.takeover.unavailable")}</p>}
                  {mode === "refused" && <p role="alert" className="text-sm text-red-700">{t("standby.takeover.refused", { ip: status.peer_ip })}</p>}
                  {mode === "normal" && <p className="text-sm text-slate-600">{t("standby.takeover.normal", { ip: status.peer_ip })}</p>}
                  {mode === "force" && (
                    <>
                      <p role="alert" className="text-sm text-amber-900">{t("standby.takeover.force", { ip: status.peer_ip })}</p>
                      <FieldGroup label={t("standby.takeover.typeLabel", { sentence: status.promote.force_sentence })}>
                        <Input autoComplete="off" autoCapitalize="none" spellCheck={false} value={sentence} onChange={(e) => setSentence(e.target.value)} />
                      </FieldGroup>
                    </>
                  )}
                  {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
                  {(mode === "normal" || mode === "force") && (
                    armed || mode === "force" ? (
                      <div className="flex flex-wrap gap-2">
                        <Button variant="danger" loading={busy} disabled={mode === "force" && !sentenceOk} onClick={() => void takeOver()}>{t("standby.takeover.confirm")}</Button>
                        {mode === "normal" && <Button variant="outline" onClick={() => setArmed(false)}>{t("common.cancel")}</Button>}
                      </div>
                    ) : (
                      <Button variant="danger" onClick={() => setArmed(true)}>{t("standby.takeover.button")}</Button>
                    )
                  )}
                </section>
              )}

              {(phase === "running" || phase === "failed" || phase === "done") && (
                <section aria-live="polite" className="space-y-2 border-t border-slate-200 pt-4">
                  {phase === "running" && <p className="flex items-center gap-2 text-sm" role="status"><Spinner size={14} /> {t("standby.running")}</p>}
                  {phase === "done" && (
                    <p className="flex items-start gap-2 text-sm text-green-800" role="status">
                      <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden="true" /> {t("standby.done")}
                    </p>
                  )}
                  {phase === "failed" && <p role="alert" className="text-sm text-red-700">{error ?? t("standby.failed", { code: String(job?.exit_code ?? "?") })}</p>}
                  {log.length > 0 && (
                    <pre className="max-h-56 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100" tabIndex={0} aria-label={t("standby.logLabel")}>{log.join("\n")}</pre>
                  )}
                  {phase === "done" && <Button href="/login">{t("standby.toLogin")}</Button>}
                  {phase === "failed" && <Button variant="outline" onClick={() => { setPhase("status"); setError(null); void loadStatus(); }}>{t("standby.back")}</Button>}
                </section>
              )}

              {phase !== "done" && (
                <div className="flex justify-between border-t border-slate-200 pt-3 text-xs text-slate-500">
                  <span>{t("standby.signedInAs", { user: status.username })}</span>
                  <button type="button" className="text-brand-600 hover:underline" onClick={() => void signOut()}>{t("standby.signOut")}</button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
