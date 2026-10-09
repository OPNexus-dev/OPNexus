"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Table, TBody, TD, TH, THead, TRow } from "@/components/ui/Table";
import { api, NetworkAction, NetworkKind, NetworkObjects } from "@/lib/api";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

export interface ObjectChange {
  kind: NetworkKind;
  action: NetworkAction;
  uuid?: string;
  preset?: Record<string, string>;
}

interface Props {
  hostId: string;
  kind: NetworkKind;
  title: string;
  reloadKey: number;
  readOnly: boolean;
  onChange: (change: ObjectChange) => void;
}

const FLAG_COLUMNS = new Set(["enabled"]);

/**
 * One table of a generic object kind (tunables, cron jobs, LAGG, ...) with its add/edit/delete
 * actions. The rows come from the firewall via the backend's reduced column list.
 */
export function NetworkObjectSection({ hostId, kind, title, reloadKey, readOnly, onChange }: Props) {
  const { t, errorMessage } = useLanguage();
  const [data, setData] = useState<NetworkObjects | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listNetworkObjects(hostId, kind)
      .then((d) => !cancelled && (setData(d), setError(null)))
      .catch((e) => !cancelled && setError(errorMessage(e, "network.loadFailed")));
    return () => {
      cancelled = true;
    };
  }, [hostId, kind, reloadKey, errorMessage]);

  // Built-in tunables carry a plain name as id (no UUID): the firewall creates an override instead of editing them.
  const isBuiltIn = (uuid: string) => kind === "tunable" && !uuid.includes("-");

  return (
    <div>
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
        <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
        <Button variant="outline" disabled={readOnly || data?.available === false} onClick={() => onChange({ kind, action: "add" })}>
          <Plus size={16} />
          {t("network.rowAdd")}
        </Button>
      </div>
      {error && <p className="px-5 py-3 text-sm text-red-600">{error}</p>}
      {!data && !error && <p className="px-5 py-4 text-sm text-slate-500">{t("common.loading")}</p>}
      {data && !data.available && <p className="px-5 py-4 text-sm text-slate-500">{t("network.objectsUnavailable")}</p>}
      {data && data.available && data.rows.length === 0 && <p className="px-5 py-4 text-sm text-slate-500">{t("network.empty")}</p>}
      {data && data.available && data.rows.length > 0 && (
        <div className="overflow-x-auto">
          <Table>
            <THead>
              <TRow>
                {data.columns.map((c) => (
                  <TH key={c}>{t(`network.field.${c}`)}</TH>
                ))}
                <TH>
                  <span className="sr-only">{t("common.actions")}</span>
                </TH>
              </TRow>
            </THead>
            <TBody>
              {data.rows.map((row) => (
                <TRow key={row.uuid}>
                  {data.columns.map((c) => (
                    <TD key={c} className={c === "description" || c === "descr" ? "" : "font-mono text-xs"}>
                      {FLAG_COLUMNS.has(c) ? (
                        <Badge tone={row[c] === "1" ? "success" : "neutral"}>{row[c] === "1" ? t("network.active") : t("common.statusDisabled")}</Badge>
                      ) : (
                        row[c] || "–"
                      )}
                    </TD>
                  ))}
                  <TD>
                    <fieldset disabled={readOnly} className="contents">
                      <div className="flex justify-end gap-1">
                        {isBuiltIn(row.uuid) ? (
                          <Button
                            variant="ghost"
                            onClick={() => onChange({ kind, action: "add", preset: { tunable: row.tunable ?? "", value: row.value ?? "" } })}
                          >
                            {t("network.override")}
                          </Button>
                        ) : (
                          <>
                            <Button variant="ghost" aria-label={t("network.rowEdit")} onClick={() => onChange({ kind, action: "set", uuid: row.uuid })}>
                              <Pencil size={14} />
                            </Button>
                            <Button variant="dangerSubtle" aria-label={t("network.rowDelete")} onClick={() => onChange({ kind, action: "del", uuid: row.uuid })}>
                              <Trash2 size={14} />
                            </Button>
                          </>
                        )}
                      </div>
                    </fieldset>
                  </TD>
                </TRow>
              ))}
            </TBody>
          </Table>
        </div>
      )}
    </div>
  );
}
