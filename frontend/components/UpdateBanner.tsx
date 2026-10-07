"use client";

import { api, VersionCheck } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useReadOnly } from "@/lib/useReadOnly";
import { ArrowUpCircle, X } from "lucide-react";
import { useEffect, useState } from "react";

const DISMISSED_KEY = "opnexus-update-dismissed";

function readDismissed(): string | null {
  try {
    return localStorage.getItem(DISMISSED_KEY);
  } catch {
    return null;
  }
}

/**
 * "Neue Version verfügbar"-Hinweis. Rein informativ: OPNexus aktualisiert sich
 * nie selbst, das macht `opnexus update` auf dem Host. Erscheint nur für
 * Administratoren (Nur-Lesen-Konten können nichts damit anfangen), nur wenn
 * der Server den Check aktiviert hat (UPDATE_CHECK_URL), und pro Version nur
 * bis zum Wegklicken — eine noch neuere Version zeigt ihn wieder.
 */
export function UpdateBanner() {
  const { t } = useLanguage();
  const readOnly = useReadOnly();
  const [check, setCheck] = useState<VersionCheck | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);

  useEffect(() => {
    setDismissed(readDismissed());
    let active = true;
    api
      .versionCheck()
      .then((result) => {
        if (active) setCheck(result);
      })
      .catch(() => {
        // Ein nicht erreichbarer Check ist kein Fehler, der den Nutzer stören soll.
      });
    return () => {
      active = false;
    };
  }, []);

  if (readOnly || !check?.update_available || !check.latest || dismissed === check.latest) return null;

  const releaseUrl = check.release_url && /^https?:\/\//.test(check.release_url) ? check.release_url : null;

  function dismiss() {
    try {
      localStorage.setItem(DISMISSED_KEY, check!.latest!);
    } catch {
      // Ohne Storage bleibt es bei "bis zum nächsten Laden ausgeblendet".
    }
    setDismissed(check!.latest);
  }

  return (
    <div className="mx-auto max-w-6xl px-4 pt-4 sm:px-6">
      <div role="status" className="flex items-start gap-3 rounded-lg border border-sky-100 bg-sky-50 p-3 text-sm text-sky-800">
        <ArrowUpCircle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
        <div className="flex-1">
          <p className="font-medium">{t("updateBanner.title", { latest: check.latest, current: check.current })}</p>
          <p className="text-xs">
            {t("updateBanner.how")}
            {releaseUrl && (
              <>
                {" "}
                <a href={releaseUrl} target="_blank" rel="noopener noreferrer" className="font-medium underline">
                  {t("updateBanner.notes")}
                </a>
              </>
            )}
          </p>
        </div>
        <button onClick={dismiss} aria-label={t("updateBanner.dismiss")} className="shrink-0 rounded p-0.5 hover:bg-black/5">
          <X size={16} />
        </button>
      </div>
    </div>
  );
}
