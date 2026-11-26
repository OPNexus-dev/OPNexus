"use client";

import { api } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** Whether the administrator made a comment mandatory when deploying (Settings > Sign-in & access). Unknown = optional. */
export function useDeployCommentRequired(): boolean {
  const [required, setRequired] = useState(false);
  useEffect(() => {
    let active = true;
    api.getGovernance().then((g) => { if (active) setRequired(!!g.require_deploy_comment); }).catch(() => {});
    return () => { active = false; };
  }, []);
  return required;
}

/** One comment for the deploy: kept in the audit entries it causes. Mandatory only when the setting says so. */
export function DeployCommentField({ value, onChange, required, disabled }: {
  value: string; onChange: (value: string) => void; required: boolean; disabled?: boolean;
}) {
  const { t } = useLanguage();
  return (
    <label className="block text-sm">
      <span className="font-medium text-slate-700">{t("rules.comment.label")} <span className="font-normal text-slate-500">({t(required ? "rules.comment.required" : "rules.comment.optional")})</span></span>
      <textarea
        className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
        rows={2} maxLength={500} value={value} disabled={disabled} required={required}
        placeholder={t("rules.comment.placeholder")} onChange={(e) => onChange(e.target.value)}
      />
      {required && !value.trim() && <span className="text-xs text-slate-500">{t("rules.comment.needed")}</span>}
    </label>
  );
}
