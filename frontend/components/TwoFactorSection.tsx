"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { api } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { ShieldCheck } from "lucide-react";
import QRCode from "qrcode";
import { useEffect, useState } from "react";

type Phase = "idle" | "password" | "scan" | "codes" | "disable";

/** Settings card: set up / disable TOTP two-factor sign-in for the current user. */
export function TwoFactorSection() {
  const { t, errorMessage } = useLanguage();
  const [status, setStatus] = useState<{ enabled: boolean; recovery_codes_left: number } | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [setup, setSetup] = useState<{ secret: string; qr: string } | null>(null);
  const [recovery, setRecovery] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    try { setStatus(await api.twoFactorStatus()); } catch (e) { setError(errorMessage(e)); }
  }
  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function reset() {
    setPhase("idle"); setPassword(""); setCode(""); setSetup(null); setRecovery([]); setSaved(false); setError("");
  }

  async function run<T>(fn: () => Promise<T>, fallback: string): Promise<T | null> {
    setBusy(true); setError("");
    try { return await fn(); } catch (e) { setError(errorMessage(e, fallback)); return null; } finally { setBusy(false); }
  }

  async function startSetup(e: React.FormEvent) {
    e.preventDefault();
    const result = await run(() => api.twoFactorSetup(password), "common.saveFailed");
    if (!result) return;
    setSetup({ secret: result.secret, qr: await QRCode.toDataURL(result.otpauth_uri, { margin: 1, width: 200 }) });
    setPassword(""); setCode(""); setPhase("scan");
  }

  async function enable(e: React.FormEvent) {
    e.preventDefault();
    const result = await run(() => api.twoFactorEnable(code.trim()), "common.saveFailed");
    if (!result) return;
    setRecovery(result.recovery_codes); setSaved(false); setPhase("codes");
    await refresh();
  }

  async function disable(e: React.FormEvent) {
    e.preventDefault();
    const result = await run(() => api.twoFactorDisable(password, code.trim()), "common.saveFailed");
    if (!result) return;
    reset(); await refresh();
  }

  function downloadCodes() {
    const blob = new Blob([`OPNexus recovery codes\n\n${recovery.join("\n")}\n`], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "opnexus-recovery-codes.txt"; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="flex items-center gap-2 text-lg font-medium">
        {t("twofa.title")}<InfoTip text={t("twofa.intro")} />
        {status && <Badge tone={status.enabled ? "success" : "neutral"}>{status.enabled ? t("twofa.on") : t("twofa.off")}</Badge>}
      </h2>

      {phase === "idle" && status && (
        <div className="flex flex-wrap items-center gap-3">
          {status.enabled ? (
            <>
              <span className="flex items-center gap-1.5 text-sm text-slate-600"><ShieldCheck size={16} className="text-emerald-600" />{t("twofa.codesLeft", { count: status.recovery_codes_left })}</span>
              <Button type="button" variant="outline" onClick={() => setPhase("disable")}>{t("twofa.disable")}</Button>
            </>
          ) : (
            <Button type="button" onClick={() => setPhase("password")}>{t("twofa.enable")}</Button>
          )}
        </div>
      )}

      {phase === "password" && (
        <form onSubmit={startSetup} className="space-y-3">
          <label className="block text-sm">
            <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("twofa.confirmPassword")}<InfoTip text={t("twofa.confirmPasswordHelp")} size={13} /></span>
            <Input type="password" autoFocus autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          <div className="flex gap-2"><Button type="submit" disabled={busy}>{t("twofa.continue")}</Button><Button type="button" variant="ghost" onClick={reset}>{t("common.cancel")}</Button></div>
        </form>
      )}

      {phase === "scan" && setup && (
        <form onSubmit={enable} className="space-y-3">
          <p className="text-sm text-slate-600">{t("twofa.scan")}</p>
          <div className="flex flex-wrap items-center gap-4">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={setup.qr} alt={t("twofa.qrAlt")} width={160} height={160} className="rounded-lg bg-white p-1" />
            <div className="text-xs text-slate-500">
              <p className="mb-1">{t("twofa.manualKey")}</p>
              <code className="break-all rounded bg-slate-100 px-2 py-1 font-mono text-sm text-slate-800">{setup.secret}</code>
            </div>
          </div>
          <label className="block text-sm">
            <span className="mb-1 block font-medium text-slate-700">{t("twofa.enterCode")}</span>
            <Input autoFocus required inputMode="numeric" autoComplete="one-time-code" maxLength={8} value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" />
          </label>
          <div className="flex gap-2"><Button type="submit" disabled={busy}>{t("twofa.activate")}</Button><Button type="button" variant="ghost" onClick={reset}>{t("common.cancel")}</Button></div>
        </form>
      )}

      {phase === "codes" && (
        <div className="space-y-3">
          <p className="flex items-center gap-1.5 text-sm font-medium text-flame-800">{t("twofa.codesTitle")}<InfoTip text={t("twofa.codesHelp")} size={13} /></p>
          <ul className="grid grid-cols-2 gap-2 rounded-xl border border-slate-200 p-3 font-mono text-sm sm:grid-cols-3">
            {recovery.map((c) => <li key={c}>{c}</li>)}
          </ul>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => void navigator.clipboard?.writeText(recovery.join("\n"))}>{t("twofa.copy")}</Button>
            <Button type="button" variant="outline" onClick={downloadCodes}>{t("twofa.download")}</Button>
          </div>
          <label className="flex items-start gap-2 text-sm"><input className="mt-0.5" type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />{t("twofa.savedAck")}</label>
          <Button type="button" disabled={!saved} onClick={reset}>{t("twofa.done")}</Button>
        </div>
      )}

      {phase === "disable" && (
        <form onSubmit={disable} className="space-y-3">
          <label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{t("twofa.confirmPassword")}</span>
            <Input type="password" autoFocus autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          <label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{t("twofa.enterCodeOrRecovery")}</span>
            <Input required autoComplete="one-time-code" autoCapitalize="none" maxLength={32} value={code} onChange={(e) => setCode(e.target.value)} /></label>
          <div className="flex gap-2"><Button type="submit" variant="danger" disabled={busy}>{t("twofa.disable")}</Button><Button type="button" variant="ghost" onClick={reset}>{t("common.cancel")}</Button></div>
        </form>
      )}

      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
    </section>
  );
}
