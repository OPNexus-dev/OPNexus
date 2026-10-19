"use client";

import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/Button";
import { NetworkBackground } from "@/components/NetworkBackground";
import { Spinner } from "@/components/ui/Spinner";
import { FieldGroup, Input } from "@/components/ui/Field";
import { api, ApiError } from "@/lib/api";
import { LOGIN_INFO_STORAGE_KEY } from "@/lib/loginInfo";
import { APP_CODENAME, APP_MAINTAINER, APP_VERSION } from "@/lib/version";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useLanguage } from "@/components/LanguageProvider";

export default function LoginPage() {
  const router = useRouter();
  const { t, errorMessage } = useLanguage();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [needCode, setNeedCode] = useState(false);
  const [code, setCode] = useState("");
  const shakeRef = useRef<HTMLDivElement>(null);
  const [sso, setSso] = useState<{ enabled: boolean; name: string | null } | null>(null);

  useEffect(() => {
    api.oidcConfig().then(setSso).catch(() => setSso(null));
    const code = new URLSearchParams(window.location.search).get("sso_error");
    if (code) setError(t(`login.sso.errors.${["no_role", "state", "token", "provider", "throttled", "disabled"].includes(code) ? code : "failed"}`));
  }, []);

  function signInWithSso() {
    window.location.href = `/api/auth/oidc/start?origin=${encodeURIComponent(window.location.origin)}`;
  }

  function shake() {
    const el = shakeRef.current;
    if (!el) return;
    el.classList.remove("login-shake");
    void el.offsetWidth; // restart the animation on repeated failures
    el.classList.add("login-shake");
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const result = await api.login(username, password, needCode ? code.trim() : undefined);
      try {
        sessionStorage.setItem(LOGIN_INFO_STORAGE_KEY, JSON.stringify(result));
      } catch {
        // Private-browsing/storage-blocked — the welcome-back banner is a
        // nice-to-have, not worth failing the login over.
      }
      setLeaving(true);
      await new Promise((resolve) => window.setTimeout(resolve, 280));
      router.push("/dashboard");
    } catch (err) {
      if (err instanceof ApiError && err.code === "TOTP_REQUIRED") {
        setNeedCode(true);
        setError(null);
        return;
      }
      setError(errorMessage(err, "login.failed"));
      shake();
    } finally {
      setLoading(false);
    }
  }

  const rise = (delay: number) => ({ animationDelay: `${delay}ms` });

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden bg-slate-50 px-4">
      <div aria-hidden="true" className="login-glow login-glow-blue pointer-events-none absolute -left-40 -top-40 h-[34rem] w-[34rem]" />
      <div aria-hidden="true" className="login-glow login-glow-orange pointer-events-none absolute -bottom-48 -right-40 h-[32rem] w-[32rem]" style={{ animationDelay: "-5s" }} />
      <NetworkBackground />
      <div className={`relative z-10 w-full max-w-sm ${leaving ? "login-leave" : ""}`}>
        <div ref={shakeRef}>
          <div className="login-card login-rise rounded-2xl p-8 shadow-xl">
            <Logo className="login-rise mb-3 h-8 w-auto" />
            <h1 className="login-rise mb-6 text-sm font-normal text-slate-500" style={rise(90)}>{t("login.prompt")}</h1>
            <form onSubmit={handleSubmit} className="space-y-4">
              {!needCode ? (
                <>
                  <div className="login-rise" style={rise(150)}>
                    <FieldGroup label={t("login.username")}>
                      <Input required autoFocus autoComplete="username" autoCapitalize="none" spellCheck={false} value={username} onChange={(e) => setUsername(e.target.value)} />
                    </FieldGroup>
                  </div>
                  <div className="login-rise" style={rise(220)}>
                    <FieldGroup label={t("login.password")}>
                      <Input required type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
                    </FieldGroup>
                  </div>
                </>
              ) : (
                <div className="space-y-2">
                  <FieldGroup label={t("login.code")}>
                    <Input required autoFocus autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} inputMode="text" maxLength={32} value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" />
                  </FieldGroup>
                  <p className="text-xs text-slate-500">{t("login.codeHint")}</p>
                  <button type="button" className="text-xs text-brand-600 hover:underline" onClick={() => { setNeedCode(false); setCode(""); setPassword(""); setError(null); }}>{t("login.back")}</button>
                </div>
              )}
              {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
              <div className="login-rise" style={rise(290)}>
                <Button type="submit" disabled={loading} className="w-full justify-center">
                  {loading && <Spinner size={14} className="text-white" />}
                  {loading ? t("login.signingIn") : t("login.signIn")}
                </Button>
              </div>
            </form>
            {sso?.enabled && !needCode && (
              <div className="login-rise mt-4 border-t border-slate-200 pt-4" style={rise(340)}>
                <Button type="button" variant="outline" onClick={signInWithSso} className="w-full justify-center">
                  {t("login.sso.button", { name: sso.name ?? "SSO" })}
                </Button>
              </div>
            )}
          </div>
        </div>
        <p className="login-rise mt-4 text-center text-xs text-slate-400" style={rise(380)}>
          {t("common.versionLabel", { version: APP_VERSION, codename: APP_CODENAME })} · {t("login.developedBy", { maintainer: APP_MAINTAINER })}
        </p>
      </div>
    </div>
  );
}
