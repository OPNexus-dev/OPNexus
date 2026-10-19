"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Field";
import { InfoTip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, Host, HostVariable } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useReadOnly } from "@/lib/useReadOnly";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

/** Site variables: values for {{NAME}} placeholders that rules, aliases and NAT entries use for this firewall. */
export function HostVariables({ host }: { host: Host }) {
  const { t, errorMessage } = useLanguage();
  const readOnly = useReadOnly();
  const toast = useToast();
  const [rows, setRows] = useState<HostVariable[] | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [added, setAdded] = useState<string[]>([]);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function apply(list: HostVariable[]) {
    setRows(list);
    setValues(Object.fromEntries(list.map((v) => [v.name, v.value ?? ""])));
    setAdded([]);
  }

  useEffect(() => {
    api.getHostVariables(host.id).then((r) => apply(r.variables)).catch((e) => setError(errorMessage(e, "variables.loadFailed")));
  }, [host.id]);

  const names = [...(rows ?? []).map((r) => r.name), ...added];
  const usage = (name: string) => rows?.find((r) => r.name === name);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const payload = Object.fromEntries(names.filter((n) => (values[n] ?? "").trim() !== "").map((n) => [n, values[n].trim()]));
      apply((await api.setHostVariables(host.id, payload)).variables);
      toast.show(t("variables.saved"), "success");
    } catch (e) {
      setError(errorMessage(e, "variables.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  function addName() {
    const name = newName.trim().toUpperCase();
    if (!name || names.includes(name)) return;
    setAdded([...added, name]);
    setNewName("");
  }

  const missing = (rows ?? []).filter((r) => r.missing && !(values[r.name] ?? "").trim()).length;

  return (
    <section className="mt-8">
      <h2 className="mb-3 flex items-center gap-2 text-lg font-semibold text-slate-900">
        {t("variables.title")}
        <InfoTip text={t("variables.intro")} />
        {missing > 0 && <Badge tone="warning">{t("variables.missing", { n: missing })}</Badge>}
      </h2>
      <Card className="p-5">
        {error && <p role="alert" className="mb-3 text-sm text-red-600">{error}</p>}
        {rows && names.length === 0 && <p className="text-sm text-slate-500">{t("variables.empty")}</p>}
        <fieldset disabled={readOnly} className="space-y-3">
          {names.map((name) => {
            const info = usage(name);
            return (
              <div key={name} className="grid items-center gap-2 sm:grid-cols-[14rem_1fr_auto]">
                <code className="font-mono text-sm text-slate-800">{`{{${name}}}`}</code>
                <div>
                  <Input value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })} placeholder={info?.missing ? t("variables.required") : ""} aria-label={name} />
                  {info && info.used_by.length > 0 && <p className="mt-1 text-xs text-slate-500">{t("variables.usedBy", { list: info.used_by.map((u) => u.label).slice(0, 4).join(", ") })}</p>}
                </div>
                <Button type="button" variant="ghost" onClick={() => { setValues({ ...values, [name]: "" }); setAdded(added.filter((n) => n !== name)); }} aria-label={t("rules.delete")}>
                  <Trash2 size={14} />
                </Button>
              </div>
            );
          })}
          <div className="flex flex-wrap items-center gap-2 pt-2">
            <Input className="max-w-[14rem]" value={newName} onChange={(e) => setNewName(e.target.value.toUpperCase())} placeholder="LAN_NET" onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addName(); } }} aria-label={t("variables.newName")} />
            <Button type="button" variant="outline" onClick={addName}><Plus size={14} />{t("variables.add")}</Button>
            <Button type="button" onClick={save} disabled={saving} loading={saving}>{t("common.save")}</Button>
          </div>
        </fieldset>
      </Card>
    </section>
  );
}
