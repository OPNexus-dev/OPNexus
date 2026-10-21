"use client";

import { LoginResult } from "@/lib/api";
import { LOGIN_INFO_STORAGE_KEY } from "@/lib/loginInfo";
import { useLanguage } from "@/components/LanguageProvider";
import { AlertTriangle, X } from "lucide-react";
import { useEffect, useState } from "react";

function formatDateTime(iso: string | null | undefined, locale: string): string | null {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return null;
  }
}

export function LoginInfoBanner() {
  const { t, locale } = useLanguage();
  const [info, setInfo] = useState<LoginResult | null>(null);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(LOGIN_INFO_STORAGE_KEY);
      sessionStorage.removeItem(LOGIN_INFO_STORAGE_KEY);
      if (raw) setInfo(JSON.parse(raw));
    } catch {
      // Ignore — no banner if storage is unavailable or the payload is malformed.
    }
  }, []);

  if (!info) return null;

  const previousLogin = formatDateTime(info.previous_login_at, locale);
  const sessionExpires = formatDateTime(info.session_expires_at, locale);
  const failedAttempts = info.failed_attempts_since_last_login ?? 0;
  const hasWarning = failedAttempts > 0;

  return (
    <div className="mx-auto max-w-6xl px-4 pt-4 sm:px-6">
      <div
        className={
          hasWarning
            ? "flex items-start gap-3 rounded-lg border border-flame-200 bg-flame-50 p-3 text-sm text-flame-800"
            : "flex items-start gap-3 rounded-lg border border-sky-100 bg-sky-50 p-3 text-sm text-sky-800"
        }
      >
        {hasWarning && <AlertTriangle size={16} className="mt-0.5 shrink-0" />}
        <div className="flex-1">
          {hasWarning && (
            <p className="font-medium">
              {t(failedAttempts === 1 ? "loginBanner.failedOne" : "loginBanner.failedMany", { count: failedAttempts })}
            </p>
          )}
          {previousLogin ? (
            <p>
              {t("loginBanner.lastLogin", { time: previousLogin })}
              {info.previous_login_ip && t("loginBanner.fromIp", { ip: info.previous_login_ip })}
            </p>
          ) : (
            <p>{t("loginBanner.welcome", { username: info.username ?? "" })}</p>
          )}
          {sessionExpires && <p className="text-xs opacity-80">{t("loginBanner.sessionUntil", { time: sessionExpires })}</p>}
        </div>
        <button onClick={() => setInfo(null)} aria-label={t("common.close")} title={t("common.close")} className="shrink-0 rounded p-0.5 hover:bg-black/5">
          <X size={16} />
        </button>
      </div>
    </div>
  );
}
