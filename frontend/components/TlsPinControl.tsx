"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { InfoTip } from "@/components/ui/Tooltip";
import { Spinner } from "@/components/ui/Spinner";
import { api, TlsProbe } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";

export type TlsPinValue = {
  /** PEM to pin; null = keep what is stored (edit) / nothing (create). */
  pem: string | null;
  /** Skip certificate verification entirely (insecure). */
  insecure: boolean;
  /** Edit only: remove the stored pin. */
  clear: boolean;
};

/**
 * Pin a firewall's certificate: fetch what it presents, show the SHA-256
 * fingerprint, and only pin after the operator confirms it matches the one
 * shown on the firewall. Works for firewalls addressed by IP or by name.
 */
export function TlsPinControl({
  baseUrl,
  currentFingerprint,
  value,
  onChange,
}: {
  baseUrl: string;
  currentFingerprint?: string | null;
  value: TlsPinValue;
  onChange: (value: TlsPinValue) => void;
}) {
  const { t, errorMessage, locale } = useLanguage();
  const [probe, setProbe] = useState<TlsProbe | null>(null);
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);

  const canProbe = baseUrl.trim().toLowerCase().startsWith("https://");
  const fingerprint = value.pem && probe ? probe.sha256 : value.clear || value.insecure ? null : currentFingerprint ?? null;

  async function fetchCertificate() {
    setProbing(true);
    setError(null);
    setConfirmed(false);
    onChange({ ...value, pem: null, clear: value.clear });
    try {
      setProbe(await api.tlsProbe(baseUrl.trim().replace(/\/+$/, "")));
    } catch (e) {
      setProbe(null);
      setError(errorMessage(e, "tls.probeFailed"));
    } finally {
      setProbing(false);
    }
  }

  function confirm(checked: boolean) {
    setConfirmed(checked);
    onChange({ pem: checked && probe ? probe.pem : null, insecure: false, clear: false });
  }

  return (
    <div className="space-y-3 rounded-xl border border-slate-200 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1 font-medium text-slate-700">
          {t("tls.title")}
          <InfoTip text={t("tls.intro")} size={13} />
        </span>
        {fingerprint ? (
          <Badge tone="success"><ShieldCheck size={12} />{t("tls.pinned")}</Badge>
        ) : value.insecure ? (
          <Badge tone="warning"><ShieldAlert size={12} />{t("tls.insecure")}</Badge>
        ) : (
          <Badge tone="warning"><ShieldAlert size={12} />{t("tls.notPinned")}</Badge>
        )}
      </div>
      {fingerprint && <p className="break-all font-mono text-xs text-slate-500">SHA-256 {fingerprint}</p>}

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" disabled={!canProbe || probing} onClick={() => void fetchCertificate()}>
          {probing && <Spinner size={13} />}
          {currentFingerprint || value.pem ? t("tls.refetch") : t("tls.fetch")}
        </Button>
        {!canProbe && <span className="text-xs text-slate-500">{t("tls.needsHttps")}</span>}
      </div>
      {error && <p role="alert" className="text-xs text-red-600">{error}</p>}

      {probe && (
        <div className="space-y-2 rounded-lg bg-slate-100 p-3">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-slate-500">{t("tls.subject")}</dt><dd>{probe.subject}</dd>
            <dt className="text-slate-500">{t("tls.issuer")}</dt><dd>{probe.self_signed ? t("tls.selfSigned") : probe.issuer}</dd>
            <dt className="text-slate-500">{t("tls.validUntil")}</dt>
            <dd className={probe.expired ? "text-red-600" : ""}>{new Date(probe.not_after).toLocaleDateString(locale)}{probe.expired ? ` · ${t("tls.expired")}` : ""}</dd>
            <dt className="text-slate-500">SHA-256</dt><dd className="break-all font-mono">{probe.sha256}</dd>
          </dl>
          <label className="flex items-start gap-2 text-xs">
            <input className="mt-0.5" type="checkbox" checked={confirmed} onChange={(e) => confirm(e.target.checked)} />
            <span className="flex items-start gap-1">{t("tls.confirmMatch")}<InfoTip text={t("tls.confirmHelp")} size={12} className="mt-0.5" /></span>
          </label>
        </div>
      )}

      <label className="flex items-start gap-2 text-xs text-slate-500">
        <input
          className="mt-0.5"
          type="checkbox"
          checked={value.insecure}
          onChange={(e) => { setConfirmed(false); onChange({ pem: null, insecure: e.target.checked, clear: e.target.checked && Boolean(currentFingerprint) }); }}
        />
        <span className="flex items-start gap-1">{t("tls.skipVerify")}<InfoTip text={t("tls.skipVerifyHelp")} size={12} className="mt-0.5" /></span>
      </label>
    </div>
  );
}
