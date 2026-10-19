"use client";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, OidcSettings } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** Single sign-on (OpenID Connect) and separation of duties. Admin only: renders nothing for other roles. */
export function OidcSection() {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [cfg, setCfg] = useState<OidcSettings | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [name, setName] = useState("SSO");
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [scopes, setScopes] = useState("openid profile email groups");
  const [claim, setClaim] = useState("groups");
  const [admin, setAdmin] = useState("");
  const [operator, setOperator] = useState("");
  const [viewer, setViewer] = useState("");
  const [operatorsDeploy, setOperatorsDeploy] = useState(true);

  function apply(d: OidcSettings) {
    setCfg(d);
    setEnabled(d.enabled);
    setName(d.display_name);
    setIssuer(d.issuer ?? "");
    setClientId(d.client_id ?? "");
    setSecret("");
    setScopes(d.scopes);
    setClaim(d.groups_claim);
    setAdmin(d.admin_group ?? "");
    setOperator(d.operator_group ?? "");
    setViewer(d.read_only_group ?? "");
  }

  useEffect(() => {
    api.getOidcSettings().then(apply).catch(() => setHidden(true));
    api.getGovernance().then((g) => setOperatorsDeploy(g.operators_may_deploy)).catch(() => {});
  }, []);

  if (hidden) return null;

  async function save(e?: React.FormEvent) {
    e?.preventDefault();
    setBusy("save");
    setError("");
    try {
      apply(await api.updateOidcSettings({
        enabled, display_name: name, issuer, client_id: clientId, scopes, groups_claim: claim,
        admin_group: admin, operator_group: operator, read_only_group: viewer, ...(secret ? { client_secret: secret } : {}),
      }));
      toast.show(t("sso.saved"), "success");
      return true;
    } catch (err) {
      setError(errorMessage(err, "common.saveFailed"));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    if (!(await save())) return;
    setBusy("test");
    try {
      await api.testOidc();
      toast.show(t("sso.testOk"), "success");
    } catch (err) {
      setError(errorMessage(err, "sso.testFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function toggleDuties(value: boolean) {
    setOperatorsDeploy(value);
    try {
      await api.updateGovernance(value);
      toast.show(t("sso.dutiesSaved"), "success");
    } catch (err) {
      setOperatorsDeploy(!value);
      setError(errorMessage(err, "common.saveFailed"));
    }
  }

  const redirectUri = typeof window === "undefined" ? "" : `${window.location.origin}/api/auth/oidc/callback`;
  const field = (label: string, value: string, set: (v: string) => void, extra?: { placeholder?: string; type?: string }) => (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      <Input value={value} onChange={(e) => set(e.target.value)} placeholder={extra?.placeholder} type={extra?.type} autoComplete="off" />
    </label>
  );

  return (
    <>
      <section id="sso" className="scroll-mt-4 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="flex items-center gap-2 text-lg font-medium">
          {t("sso.title")}
          <InfoTip text={t("sso.intro")} />
        </h2>
        <form onSubmit={save} className="space-y-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            {t("sso.enable")}
          </label>
          <fieldset disabled={!enabled} className="space-y-3 disabled:opacity-50">
            <p className="rounded-md bg-slate-50 p-2 text-xs text-slate-600">
              {t("sso.redirectHint")} <code className="break-all font-mono">{redirectUri}</code>
            </p>
            <div className="grid grid-cols-2 gap-3">
              {field(t("sso.name"), name, setName)}
              {field(t("sso.issuer"), issuer, setIssuer, { placeholder: "https://idp.example.com/realms/main" })}
              {field(t("sso.clientId"), clientId, setClientId)}
              <label className="block text-sm">
                <span className="mb-1 block font-medium text-slate-700">
                  {t("sso.clientSecret", { state: cfg?.client_secret_set ? t("auditExport.isSet") : t("auditExport.notSet") })}
                </span>
                <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={cfg?.client_secret_set ? t("auditExport.keep") : t("auditExport.optional")} autoComplete="new-password" />
              </label>
              {field(t("sso.scopes"), scopes, setScopes)}
              {field(t("sso.claim"), claim, setClaim)}
            </div>
            <h3 className="pt-1 text-sm font-medium text-slate-700">{t("sso.mapping")}</h3>
            <div className="grid grid-cols-3 gap-3">
              {field(t("users.roleAdmin"), admin, setAdmin)}
              {field(t("users.roleOperator"), operator, setOperator)}
              {field(t("users.roleReadOnly"), viewer, setViewer)}
            </div>
            <p className="text-xs text-slate-500">{t("sso.mappingHint")}</p>
          </fieldset>
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <div className="flex gap-2">
            <Button type="submit" disabled={busy !== null || !cfg} loading={busy === "save"}>{t("common.save")}</Button>
            <Button type="button" variant="ghost" disabled={busy !== null || !cfg || !issuer} loading={busy === "test"} onClick={test}>{t("sso.test")}</Button>
          </div>
        </form>
      </section>

      <section id="duties" className="scroll-mt-4 space-y-3 rounded-xl border border-slate-200 bg-white p-5">
        <h2 className="flex items-center gap-2 text-lg font-medium">
          {t("sso.dutiesTitle")}
          <InfoTip text={t("sso.dutiesIntro")} />
        </h2>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={operatorsDeploy} onChange={(e) => toggleDuties(e.target.checked)} />
          {t("sso.operatorsDeploy")}
        </label>
      </section>
    </>
  );
}
