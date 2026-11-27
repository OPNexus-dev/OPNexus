"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Column, DataTable } from "@/components/ui/DataTable";
import { api, NetworkAction, NetworkKind, NetworkObjects } from "@/lib/api";
import { Pencil, Plus, SlidersHorizontal, Trash2 } from "lucide-react";
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
        <DataTable
          bare
          label={title}
          rows={data.rows}
          rowKey={(row) => row.uuid}
          columns={data.columns.map((c, index): Column<(typeof data.rows)[number]> => ({
            key: c,
            header: t(`network.field.${c}`),
            // The first column names the object, the next two stay on a card/tablet row, the rest is behind "Details".
            priority: index === 0 ? "primary" : index <= 2 ? "high" : "medium",
            isTitle: index === 0,
            mono: !(c === "description" || c === "descr"),
            cell: (row) => FLAG_COLUMNS.has(c)
              ? <Badge tone={row[c] === "1" ? "success" : "neutral"}>{row[c] === "1" ? t("network.active") : t("common.statusDisabled")}</Badge>
              : row[c] || "–",
          }))}
          actions={(row) => isBuiltIn(row.uuid)
            ? [{ key: "override", label: t("network.override"), icon: SlidersHorizontal, disabled: readOnly, onSelect: () => onChange({ kind, action: "add", preset: { tunable: row.tunable ?? "", value: row.value ?? "" } }) }]
            : [
                { key: "edit", label: t("network.rowEdit"), icon: Pencil, disabled: readOnly, onSelect: () => onChange({ kind, action: "set", uuid: row.uuid }) },
                { key: "delete", label: t("network.rowDelete"), icon: Trash2, tone: "danger", disabled: readOnly, onSelect: () => onChange({ kind, action: "del", uuid: row.uuid }) },
              ]}
        />
      )}
    </div>
  );
}
