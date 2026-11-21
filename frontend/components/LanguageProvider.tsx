"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { usePathname } from "next/navigation";
import { api } from "@/lib/api";
import { detectLanguage, isLanguage, Language, translate, translateReasonParams, translateServerParams, translateServerText, TranslationParams } from "@/lib/i18n";

interface LanguageContextValue {
  language: Language;
  locale: string;
  setLanguage: (language: Language) => void;
  t: (key: string, params?: TranslationParams) => string;
  ts: (text: string) => string;
  errorMessage: (error: unknown, fallbackKey?: string) => string;
}

const LanguageContext = createContext<LanguageContextValue>({
  language: "de",
  locale: "de-DE",
  setLanguage: () => {},
  t: (key) => key,
  ts: (text) => text,
  errorMessage: () => "common.requestFailed",
});

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [language, setValue] = useState<Language>("en");
  const login = usePathname() === "/login";

  const setLanguage = useCallback((value: Language) => {
    setValue(value);
    document.documentElement.lang = value;
    try { localStorage.setItem("opnexus-language", value); } catch {}
  }, []);

  // Saved choice of this browser, otherwise the browser language. Nothing is stored here: only an
  // explicit choice (settings page, setup assistant) or the account setting writes the language down.
  useEffect(() => {
    const value = detectLanguage();
    setValue(value);
    document.documentElement.lang = value;
  }, []);

  useEffect(() => {
    if (login) return;
    let active = true;
    api.me().then((user) => user.authenticated ? api.settings() : null)
      .then((data) => { if (active && data && isLanguage(data.language)) setLanguage(data.language); })
      .catch(() => {});
    return () => { active = false; };
  }, [login, setLanguage]);

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === "opnexus-language" && isLanguage(event.newValue)) {
        setValue(event.newValue);
        document.documentElement.lang = event.newValue;
      }
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  const t = useCallback((key: string, params?: TranslationParams) => translate(language, key, params), [language]);
  const ts = useCallback((text: string) => translateServerText(language, text), [language]);
  const errorMessage = useCallback((error: unknown, fallbackKey = "common.requestFailed") => {
    if (error && typeof error === "object" && "code" in error && typeof error.code === "string") {
      const params = "params" in error && error.params && typeof error.params === "object"
        ? error.params as Record<string, string>
        : undefined;
      const readable = translateReasonParams(language, error.code, translateServerParams(language, params));
      return translate(language, `errors.${error.code}`, readable as TranslationParams | undefined);
    }
    return error instanceof Error ? error.message : translate(language, fallbackKey);
  }, [language]);

  const locale = language === "en" ? "en-GB" : "de-DE";
  const value = useMemo(() => ({ language, locale, setLanguage, t, ts, errorMessage }), [language, locale, setLanguage, t, ts, errorMessage]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export const useLanguage = () => useContext(LanguageContext);
