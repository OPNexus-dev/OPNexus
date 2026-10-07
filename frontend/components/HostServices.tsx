"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { api, Host, OpnsenseService } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { useReadOnly } from "@/lib/useReadOnly";
import { Play, RotateCw, Square } from "lucide-react";
import { useEffect, useState } from "react";

export function HostServices({ host }: { host: Host }) {
  const readOnly = useReadOnly();
  const toast = useToast();
  const { t, errorMessage } = useLanguage();
  const [services, setServices] = useState<OpnsenseService[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    try {
      const { rows } = await api.listServices(host.id);
      setServices(rows);
      setError(null);
    } catch (e) {
      setError(errorMessage(e, "hostServices.loadFailed"));
    }
  }

  useEffect(() => {
    load();
  }, [host.id]);

  // svc.id is OPNsense's own combined service identifier (name plus, for
  // multi-instance plugins, an instance suffix) — used as-is as the `name`
  // path segment. A genuine split of name vs. instance id is deferred until
  // this write path gets its first live verification (see README).
  async function act(svc: OpnsenseService, action: "start" | "stop" | "restart") {
    if (
      !confirm(
        t("hostServices.confirm", { name: svc.name, verb: t(`hostServices.verb.${action}`) })
      )
    )
      return;
    setBusy(svc.id);
    try {
      const fn = action === "start" ? api.startService : action === "stop" ? api.stopService : api.restartService;
      await fn(host.id, svc.id);
      toast.show(t("hostServices.done", { name: svc.name, result: t(`hostServices.result.${action}`) }), "success");
      await load();
    } catch (e) {
      toast.show(errorMessage(e, "hostServices.actionFailed"), "danger");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h2 className="mb-3 mt-8 text-lg font-semibold text-slate-900">{t("hostServices.title")}</h2>
      <Card className="mb-8 overflow-x-auto">
        {error && <p className="px-5 py-4 text-sm text-red-600">{error}</p>}
        {!error && !services && (
          <div className="space-y-3 p-5">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        )}
        {services && services.length === 0 && (
          <p className="px-5 py-6 text-center text-slate-400">{t("hostServices.empty")}</p>
        )}
        {services && services.length > 0 && (
          <fieldset disabled={readOnly} className="contents">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-5 py-3 font-medium">{t("hostServices.service")}</th>
                  <th className="px-5 py-3 font-medium">{t("hostServices.description")}</th>
                  <th className="px-5 py-3 font-medium">{t("hostServices.status")}</th>
                  <th className="px-5 py-3 font-medium"><span className="sr-only">{t("common.actions")}</span></th>
                </tr>
              </thead>
              <tbody>
                {services.map((svc) => {
                  const running = Boolean(svc.running);
                  const locked = Boolean(svc.locked);
                  return (
                    <tr key={svc.id} className="border-b border-slate-100 last:border-0">
                      <td className="px-5 py-3 font-mono font-medium text-slate-900">{svc.name}</td>
                      <td className="px-5 py-3 text-slate-500">{svc.description}</td>
                      <td className="px-5 py-3">
                        {running ? <Badge tone="success">{t("hostServices.running")}</Badge> : <Badge tone="neutral">{t("hostServices.stopped")}</Badge>}
                      </td>
                      <td className="px-5 py-3 text-right">
                        <div className="flex justify-end gap-2">
                          {!running && (
                            <Button
                              variant="ghost"
                              disabled={busy === svc.id || locked}
                              title={locked ? t("hostServices.locked") : t("hostServices.startTitle")}
                              onClick={() => act(svc, "start")}
                            >
                              <Play size={14} />
                            </Button>
                          )}
                          {running && (
                            <Button
                              variant="ghost"
                              disabled={busy === svc.id || locked}
                              title={locked ? t("hostServices.locked") : t("hostServices.stopTitle")}
                              onClick={() => act(svc, "stop")}
                            >
                              <Square size={14} />
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            disabled={busy === svc.id || locked}
                            title={locked ? t("hostServices.locked") : t("hostServices.restartTitle")}
                            onClick={() => act(svc, "restart")}
                          >
                            <RotateCw size={14} />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </fieldset>
        )}
      </Card>
    </>
  );
}
