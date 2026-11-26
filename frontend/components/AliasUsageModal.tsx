"use client";

import { api, AliasUsage } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Loading } from "@/components/ui/Loading";
import { Modal } from "@/components/ui/Modal";
import { useLanguage } from "@/components/LanguageProvider";
import { useEffect, useState } from "react";

/** "Used by" for a central alias: the managed rules, NAT entries and other aliases that refer to it (database only). */
export function AliasUsageModal({ aliasId, aliasName, onClose }: { aliasId: string; aliasName: string; onClose: () => void }) {
  const { t, errorMessage } = useLanguage();
  const [data, setData] = useState<AliasUsage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.aliasUsage(aliasId).then((d) => { if (active) setData(d); }).catch((e) => { if (active) setError(errorMessage(e, "review.loadFailed")); });
    return () => { active = false; };
  }, [aliasId, errorMessage]);

  const field = (f: string) => (`fields.${f}` !== t(`fields.${f}`) ? t(`fields.${f}`) : f);
  return (
    <Modal title={`${t("aliases.usage.title")}: ${aliasName}`} onClose={onClose}>
      {error && <p role="alert" className="mb-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {!data && !error && <Loading className="py-4">{t("aliases.usage.loading")}</Loading>}
      {data && data.items.length === 0 && <p className="text-sm text-slate-600">{t("aliases.usage.empty")}</p>}
      {data && data.items.length > 0 && (
        <ul className="space-y-2">
          {data.items.map((item) => (
            <li key={`${item.kind}-${item.id}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 p-2 text-sm">
              <Badge tone="neutral">{t(`aliases.usage.kind.${item.kind}`)}</Badge>
              <strong className="break-words">{item.name}</strong>
              <span className="text-xs text-slate-500">{item.fields.map(field).join(", ")}</span>
            </li>
          ))}
        </ul>
      )}
      {data?.truncated && <p className="mt-2 text-xs text-slate-500">{t("aliases.usage.more")}</p>}
      <p className="mt-3 text-xs text-slate-500">{t("aliases.usage.note")}</p>
    </Modal>
  );
}
