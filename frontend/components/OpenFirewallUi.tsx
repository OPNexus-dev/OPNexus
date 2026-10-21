"use client";

import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/Tooltip";
import { useToast } from "@/components/ui/Toast";
import { api, GuiAccessSettings, Host } from "@/lib/api";
import { useLanguage } from "@/components/LanguageProvider";
import { MonitorUp } from "lucide-react";
import { useEffect, useState } from "react";

/** Why this firewall cannot be opened through OPNexus (null = eligible); mirrors the server's host_refusal(). */
function hostReason(host: Host): "disabled" | "https" | "tls" | null {
  if (!host.enabled) return "disabled";
  if (!host.base_url.trim().toLowerCase().startsWith("https://")) return "https";
  if (!host.verify_tls) return "tls";
  return null;
}

/** "Open interface" button on the host page. Admins only: other roles see nothing (the server refuses them as well). */
export function OpenFirewallUi({ host }: { host: Host }) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const [admin, setAdmin] = useState(false);
  const [cfg, setCfg] = useState<GuiAccessSettings | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    api.me().then((me) => {
      if (!active || me.role !== "admin") return;
      setAdmin(true);
      api.getGuiAccess().then((c) => active && setCfg(c)).catch(() => {});
    }).catch(() => {});
    return () => { active = false; };
  }, []);

  if (!admin) return null;

  let reason: string | null = null;
  if (!cfg) reason = null;
  else if (!cfg.available) reason = t(`guiAccess.reason.${cfg.unavailable_reasons[0]}`);
  else if (!cfg.enabled) reason = t("guiOpen.reason.off");
  else {
    const r = hostReason(host);
    if (r) reason = t(`guiOpen.reason.host_${r}`);
  }

  async function open() {
    setBusy(true);
    try {
      const ticket = await api.createGuiTicket(host.id);
      window.open(ticket.url, "_blank", "noopener,noreferrer");
      toast.show(t("guiOpen.opened", { slot: ticket.slot }), "info");
    } catch (e) {
      toast.show(errorMessage(e, "guiOpen.failed"), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Tooltip content={reason ?? t("guiOpen.tip")} focusable={Boolean(reason)}>
      <Button type="button" variant="outline" onClick={open} loading={busy} disabled={!cfg || Boolean(reason)}>
        <MonitorUp size={14} aria-hidden="true" />
        {t("guiOpen.button")}
      </Button>
    </Tooltip>
  );
}
