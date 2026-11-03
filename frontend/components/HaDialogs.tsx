"use client";

// Dialogs of the pair card in Settings > System (docs/DESIGN-HA-UI.md section 5): show the pairing code of a future standby, set a pair
// up (preflight first, then the real run), run an action on the primary. The work is done by the updater of the node; the dialogs start a
// job, follow its log and say what happened. Destructive steps ask for the account's password again.
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { FieldGroup, Input } from "@/components/ui/Field";
import { Spinner } from "@/components/ui/Spinner";
import { useLanguage } from "@/components/LanguageProvider";
import { api, HaAction, StandbyJob } from "@/lib/api";
import { CheckCircle2, Copy } from "lucide-react";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

const POLL_MS = 2000;
const MAX_SILENT_POLLS = 90;   // the node restarts services (and, for a switch or a rebuild, this very backend) on the way

/** Follows the last HA job of this node: its log while it runs, its result at the end. */
export function HaJobView({ onFinished }: { onFinished?: (job: StandbyJob) => void }) {
  const { t } = useLanguage();
  const [job, setJob] = useState<StandbyJob | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [lost, setLost] = useState(false);
  const finished = useRef(false);

  useEffect(() => {
    let silent = 0;
    let stopped = false;
    const tick = async () => {
      try {
        const next = await api.haJob();
        silent = 0;
        setLost(false);
        setLog(next.log);
        setJob(next.job);
        if (next.job && next.job.state !== "running" && !finished.current) {
          finished.current = true;
          onFinished?.(next.job);
        }
      } catch {
        silent += 1;
        if (silent > MAX_SILENT_POLLS) setLost(true);
      }
    };
    void tick();
    const id = setInterval(() => { if (!stopped && !finished.current) void tick(); }, POLL_MS);
    return () => { stopped = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const running = !job || job.state === "running";
  return (
    <div aria-live="polite" className="space-y-2">
      {lost ? <p role="alert" className="text-sm text-red-700">{t("settings.ha.manage.lost")}</p>
        : running ? <p role="status" className="flex items-center gap-2 text-sm"><Spinner size={14} /> {t("settings.ha.manage.running")}</p>
        : job?.state === "succeeded" ? <p role="status" className="flex items-center gap-2 text-sm text-green-800"><CheckCircle2 size={16} aria-hidden="true" /> {t("settings.ha.manage.succeeded")}</p>
        : <p role="alert" className="text-sm text-red-700">{t("settings.ha.manage.failed", { code: String(job?.exit_code ?? "?") })}</p>}
      {log.length > 0 && (
        <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-3 text-xs text-slate-100" tabIndex={0} aria-label={t("settings.ha.manage.logLabel")}>{log.join("\n")}</pre>
      )}
    </div>
  );
}

/** Shows the pairing code of this node as the future standby (single use, 30 minutes). */
export function PrepareDialog({ onClose }: { onClose: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [ip, setIp] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState<{ code: string; expires_in: number } | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.haSuggest().then((s) => setIp((current) => current || s.node_ip)).catch(() => undefined);
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setCode(await api.haPrepare({ password, ip }));
      setPassword("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code.code);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <Modal title={t("settings.ha.manage.prepare.title")} onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-slate-600">{t("settings.ha.manage.prepare.intro")}</p>
        {!code ? (
          <form onSubmit={submit} className="space-y-3">
            <FieldGroup label={t("settings.ha.manage.prepare.ip")} hint={t("settings.ha.manage.prepare.ipHint")}>
              <Input required inputMode="decimal" autoComplete="off" spellCheck={false} value={ip} onChange={(e) => setIp(e.target.value)} placeholder="192.168.2.21" />
            </FieldGroup>
            <FieldGroup label={t("settings.ha.manage.password")} hint={t("settings.ha.manage.passwordHint")}>
              <Input required type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </FieldGroup>
            {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={onClose}>{t("common.cancel")}</Button>
              <Button type="submit" loading={busy}>{t("settings.ha.manage.prepare.create")}</Button>
            </div>
          </form>
        ) : (
          <div className="space-y-3">
            <p className="text-sm font-medium">{t("settings.ha.manage.prepare.yourCode", { minutes: Math.round(code.expires_in / 60) })}</p>
            <pre data-testid="ha-code" className="select-all whitespace-pre-wrap break-all rounded-lg border border-slate-300 bg-slate-50 p-3 text-sm" tabIndex={0}>{code.code}</pre>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" onClick={() => void copy()}><Copy size={14} aria-hidden="true" /> {copied ? t("settings.ha.manage.prepare.copied") : t("settings.ha.manage.prepare.copy")}</Button>
              <Button variant="ghost" onClick={() => setCode(null)}>{t("settings.ha.manage.prepare.again")}</Button>
            </div>
            <p className="text-xs text-slate-500">{t("settings.ha.manage.prepare.next")}</p>
            <div className="flex justify-end"><Button onClick={onClose}>{t("common.close")}</Button></div>
          </div>
        )}
      </div>
    </Modal>
  );
}

type SetupPhase = "form" | "checking" | "checked" | "running" | "done" | "failed";

/** Sets the pair up with this node as the primary: pairing code and VIP, preflight, then the real run. */
export function SetupDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [phase, setPhase] = useState<SetupPhase>("form");
  const [code, setCode] = useState("");
  const [vip, setVip] = useState("");
  const [node, setNode] = useState("");
  const [peerNode, setPeerNode] = useState("");
  const [nodeIp, setNodeIp] = useState("");
  const [vipDev, setVipDev] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const body = useCallback((check: boolean) => ({
    code: code.trim(), vip: vip.trim(), check,
    ...(node.trim() ? { node: node.trim() } : {}), ...(peerNode.trim() ? { peer_node: peerNode.trim() } : {}),
    ...(nodeIp.trim() ? { node_ip: nodeIp.trim() } : {}), ...(vipDev.trim() ? { vip_dev: vipDev.trim() } : {}),
  }), [code, vip, node, peerNode, nodeIp, vipDev]);

  async function start(check: boolean, event?: FormEvent) {
    event?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.haInit(check ? body(true) : { ...body(false), password });
      setPassword("");
      setPhase(check ? "checking" : "running");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const finished = useCallback((job: StandbyJob) => {
    setPhase((current) => (job.state === "succeeded" ? (current === "checking" ? "checked" : "done") : "failed"));
    if (job.state === "succeeded") onDone();
  }, [onDone]);

  return (
    <Modal title={t("settings.ha.manage.setup.title")} onClose={onClose} wide>
      <div className="space-y-4">
        {phase === "form" && (
          <form onSubmit={(e) => void start(true, e)} className="space-y-3">
            <p className="text-sm text-slate-600">{t("settings.ha.manage.setup.intro")}</p>
            <FieldGroup label={t("settings.ha.manage.setup.code")} hint={t("settings.ha.manage.setup.codeHint")}>
              <textarea required rows={3} spellCheck={false} autoCapitalize="none" autoComplete="off" value={code} onChange={(e) => setCode(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" placeholder="OPNX1-…" />
            </FieldGroup>
            <FieldGroup label={t("settings.ha.manage.setup.vip")} hint={t("settings.ha.manage.setup.vipHint")}>
              <Input required inputMode="decimal" autoComplete="off" spellCheck={false} value={vip} onChange={(e) => setVip(e.target.value)} placeholder="192.168.2.22/24" />
            </FieldGroup>
            <button type="button" className="text-xs text-brand-600 hover:underline" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>
              {advanced ? t("settings.ha.manage.setup.hideAdvanced") : t("settings.ha.manage.setup.showAdvanced")}
            </button>
            {advanced && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <FieldGroup label={t("settings.ha.manage.setup.node")}><Input autoComplete="off" value={node} onChange={(e) => setNode(e.target.value)} placeholder="a" /></FieldGroup>
                <FieldGroup label={t("settings.ha.manage.setup.peerNode")}><Input autoComplete="off" value={peerNode} onChange={(e) => setPeerNode(e.target.value)} placeholder="b" /></FieldGroup>
                <FieldGroup label={t("settings.ha.manage.setup.nodeIp")} hint={t("settings.ha.manage.setup.autoHint")}><Input autoComplete="off" value={nodeIp} onChange={(e) => setNodeIp(e.target.value)} /></FieldGroup>
                <FieldGroup label={t("settings.ha.manage.setup.vipDev")} hint={t("settings.ha.manage.setup.autoHint")}><Input autoComplete="off" value={vipDev} onChange={(e) => setVipDev(e.target.value)} /></FieldGroup>
              </div>
            )}
            {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={onClose}>{t("common.cancel")}</Button>
              <Button type="submit" loading={busy}>{t("settings.ha.manage.setup.check")}</Button>
            </div>
          </form>
        )}

        {(phase === "checking" || phase === "checked" || phase === "running" || phase === "done" || phase === "failed") && (
          <>
            <h3 className="text-sm font-medium">{phase === "checking" || phase === "checked" ? t("settings.ha.manage.setup.checkTitle") : t("settings.ha.manage.setup.runTitle")}</h3>
            <HaJobView key={phase === "checking" || phase === "checked" ? "check" : "run"} onFinished={finished} />
          </>
        )}

        {phase === "checked" && (
          <form onSubmit={(e) => void start(false, e)} className="space-y-3 border-t border-slate-200 pt-4">
            <p role="alert" className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">{t("settings.ha.manage.setup.warning")}</p>
            <FieldGroup label={t("settings.ha.manage.password")} hint={t("settings.ha.manage.passwordHint")}>
              <Input required type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </FieldGroup>
            {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={onClose}>{t("common.cancel")}</Button>
              <Button type="submit" variant="danger" loading={busy}>{t("settings.ha.manage.setup.run")}</Button>
            </div>
          </form>
        )}
        {phase === "failed" && (
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => { setPhase("form"); setError(null); }}>{t("settings.ha.manage.back")}</Button>
            <Button onClick={onClose}>{t("common.close")}</Button>
          </div>
        )}
        {phase === "done" && <div className="flex justify-end"><Button onClick={onClose}>{t("common.close")}</Button></div>}
      </div>
    </Modal>
  );
}

const ACTION_KEYS: Record<Exclude<HaAction, "dissolve_local">, { danger: boolean; gap: boolean }> = {
  sync: { danger: false, gap: false },
  switch: { danger: true, gap: true },
  rebuild: { danger: true, gap: false },
  dissolve: { danger: true, gap: true },
};

/** An action on the primary: sync now, switch to the partner, rebuild the standby, dissolve the pair. */
export function ActionDialog({ action, onClose, onDone }: { action: Exclude<HaAction, "dissolve_local">; onClose: () => void; onDone: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [password, setPassword] = useState("");
  const [localOnly, setLocalOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const [result, setResult] = useState<StandbyJob | null>(null);
  const meta = ACTION_KEYS[action];

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.haAction({ password, action: action === "dissolve" && localOnly ? "dissolve_local" : action });
      setPassword("");
      setStarted(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const finished = useCallback((job: StandbyJob) => { setResult(job); onDone(); }, [onDone]);

  return (
    <Modal title={t(`settings.ha.manage.actions.${action}.title`)} onClose={onClose}>
      <div className="space-y-4">
        {!started ? (
          <form onSubmit={submit} className="space-y-3">
            <p className="text-sm text-slate-600">{t(`settings.ha.manage.actions.${action}.text`)}</p>
            {meta.gap && <p className="text-sm text-amber-900">{t(`settings.ha.manage.actions.${action}.gap`)}</p>}
            {action === "dissolve" && (
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={localOnly} onChange={(e) => setLocalOnly(e.target.checked)} className="mt-1" />
                <span>{t("settings.ha.manage.actions.dissolve.localOnly")}</span>
              </label>
            )}
            <FieldGroup label={t("settings.ha.manage.password")} hint={t("settings.ha.manage.passwordHint")}>
              <Input required type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </FieldGroup>
            {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={onClose}>{t("common.cancel")}</Button>
              <Button type="submit" variant={meta.danger ? "danger" : "primary"} loading={busy}>{t(`settings.ha.manage.actions.${action}.run`)}</Button>
            </div>
          </form>
        ) : (
          <>
            <HaJobView onFinished={finished} />
            {result?.state === "succeeded" && action === "switch" && <p className="text-sm text-slate-600">{t("settings.ha.manage.actions.switch.after")}</p>}
            <div className="flex justify-end"><Button onClick={onClose}>{t("common.close")}</Button></div>
          </>
        )}
      </div>
    </Modal>
  );
}
