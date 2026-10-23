"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { ConfidenceBadge, KIND_TONE, NoteBadge } from "@/components/CleanupBadges";
import { RuleHistoryModal } from "@/components/RuleHistoryModal";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Field";
import { Loading } from "@/components/ui/Loading";
import { Modal } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { api, CleanupActionKind, CleanupOperation, CleanupPlanItem, CleanupPlanNode, CleanupPreview } from "@/lib/api";
import { ACK_TEXT_VERSION, formatClock, secondsUntil } from "@/lib/cleanup";
import { useNotAdmin } from "@/lib/useReadOnly";
import { History, RefreshCw, ShieldCheck, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

/** What the user picked: the kind of run and the rules (needsAck = "probable", an explicit acknowledgement each). */
export interface CleanupRequest {
  kind: CleanupActionKind;
  rules: { uuid: string; label: string; needsAck: boolean }[];
}

const ACTION_TONE = { disable: "warning", enable: "info", delete: "danger" } as const;

function duration(t: (key: string, params?: Record<string, string | number>) => string, seconds: number): string {
  return seconds >= 60 ? t("cleanup.dur.minutes", { n: Math.round(seconds / 60) }) : t("cleanup.dur.seconds", { n: seconds });
}

function PlanItem({ item, kind, sources, coverName, acknowledged, onHistory }: {
  item: CleanupPlanItem; kind: CleanupActionKind; sources: string[]; coverName: (uuid: string) => string | null;
  acknowledged: boolean; onHistory: () => void;
}) {
  const { t, locale } = useLanguage();
  const protection = item.protection;
  const cover = item.cover_uuid ? coverName(item.cover_uuid) : null;
  const notes = Array.from(new Set([...item.notes, ...item.categories.flatMap((c) => c.notes ?? [])]));
  return (
    <li className="rounded-xl border border-slate-200 p-3 text-sm">
      <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
        <span className="min-w-0 flex-1 break-words font-medium text-slate-900">{item.description}</span>
        <button
          type="button"
          onClick={onHistory}
          className="shrink-0 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
          aria-label={t("cleanup.history", { rule: item.description })}
          title={t("cleanup.historyTitle")}
        >
          <History size={15} aria-hidden="true" />
        </button>
        <Badge tone={ACTION_TONE[kind]}>{t(`cleanup.action.${kind}`)}</Badge>
      </div>
      {item.categories.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {item.categories.map((category) => (
            <span key={category.kind} className="inline-flex items-center gap-1">
              <Tooltip content={t(`cleanup.category.${category.kind}_hint`)} focusable>
                <Badge tone={KIND_TONE[category.kind]}>{t(`cleanup.category.${category.kind}`)}</Badge>
              </Tooltip>
              <ConfidenceBadge confidence={category.confidence} />
            </span>
          ))}
        </div>
      )}
      {cover && <p className="mt-1.5 break-words text-xs text-slate-500">{t("cleanup.detail.by", { rule: cover })}</p>}
      {item.basis?.type === "confirmed" && item.basis.confirmed_at && (
        <p className="mt-1.5 text-xs text-slate-500">{t("cleanup.preview.basisConfirmed", { when: new Date(item.basis.confirmed_at).toLocaleDateString(locale) })}</p>
      )}
      {item.basis?.type === "disabled_long" && (
        <p className="mt-1.5 text-xs text-slate-500">{item.basis.since && !Number.isNaN(Date.parse(item.basis.since))
          ? t("cleanup.detail.disabledSince", { when: new Date(item.basis.since).toLocaleDateString(locale) })
          : t("cleanup.preview.basisDisabled")}</p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {protection && (
          <Tooltip
            content={t(`cleanup.protect.${protection.result}_hint`, { sources: sources.length ? sources.join(", ") : t("cleanup.protect.sourcesHidden"), queries: protection.queries })}
            focusable
          >
            <Badge tone={protection.result === "false" ? "success" : "warning"}>
              {protection.result === "false" ? <ShieldCheck size={12} aria-hidden="true" /> : <TriangleAlert size={12} aria-hidden="true" />}
              {t(`cleanup.protect.${protection.result}`)}
            </Badge>
          </Tooltip>
        )}
        {item.external && (
          <Tooltip content={t("cleanup.preview.externalHint")} focusable><Badge tone="warning">{t("cleanup.preview.external")}</Badge></Tooltip>
        )}
        {notes.map((code) => <NoteBadge key={code} code={code} />)}
        {acknowledged && <Badge tone="neutral">{t("cleanup.preview.acknowledged")}</Badge>}
      </div>
    </li>
  );
}

/**
 * Select -> (acknowledge) -> preview -> roll out. The preview is recomputed from the live firewall(s) by the backend,
 * valid for five minutes and bound to whoever created it; only an admin may roll it out (hostname typed). The
 * confirmation and the automatic revert afterwards live in the operation banner of the tab.
 */
export function CleanupPreviewModal({ host, request, unusedDays, coverName, onClose, onDeployed }: {
  host: { id: string; name: string };
  request: CleanupRequest;
  unusedDays: number;
  coverName: (uuid: string) => string | null;
  onClose: () => void;
  onDeployed: (operation: CleanupOperation) => void;
}) {
  const { t, errorMessage } = useLanguage();
  const toast = useToast();
  const notAdmin = useNotAdmin();
  const [forced, setForced] = useState<string[]>([]);
  const [acked, setAcked] = useState<string[]>([]);
  const [phase, setPhase] = useState<"ack" | "loading" | "plan" | "deploying">(request.rules.some((r) => r.needsAck) ? "ack" : "loading");
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmName, setConfirmName] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const [history, setHistory] = useState<CleanupPlanItem | null>(null);
  const [historyNode, setHistoryNode] = useState<CleanupPlanNode | null>(null);
  const spent = useRef(false);
  const previewId = useRef<string | null>(null);
  const nameErrorId = useId();

  const needsAck = useCallback((uuid: string) => forced.includes(uuid) || request.rules.some((r) => r.uuid === uuid && r.needsAck), [forced, request.rules]);
  const askAck = request.rules.filter((r) => needsAck(r.uuid));
  const allAcked = askAck.every((r) => acked.includes(r.uuid));

  const compute = useCallback(async (ackList: string[]) => {
    setPhase("loading");
    setError(null);
    setPreview(null);
    spent.current = false;
    try {
      const result = await api.previewCleanup(host.id, {
        items: request.rules.map((r) => ({ uuid: r.uuid, kind: request.kind, ack: ackList.includes(r.uuid) })),
        unused_days: unusedDays,
        ack_text_version: ACK_TEXT_VERSION,
      });
      previewId.current = result.preview_id;
      setPreview(result);
      setNow(Date.now());
      setPhase("plan");
    } catch (e) {
      const code = e && typeof e === "object" && "code" in e ? (e as { code?: string }).code : undefined;
      const params = e && typeof e === "object" && "params" in e ? (e as { params?: Record<string, string> }).params : undefined;
      if (code === "BEREINIGUNG_BESTAETIGUNG_FEHLT" && params?.uuids) {
        // The backend sees more reasons for caution than the list did (e.g. an outside change): ask for those too.
        setForced((current) => Array.from(new Set([...current, ...params.uuids.split(",").map((u) => u.trim())])));
        setPhase("ack");
      } else {
        setPhase("plan");
      }
      setError(errorMessage(e, "cleanup.preview.failed"));
    }
  }, [host.id, request, unusedDays, errorMessage]);

  useEffect(() => {
    if (!request.rules.some((r) => r.needsAck)) void compute([]);
    // Only once at open: later recomputation is the user's explicit action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (phase !== "plan" || !preview) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [phase, preview]);

  // An unused preview is dropped on the way out (a new one replaces it anyway; this keeps the list tidy).
  useEffect(() => () => {
    if (!spent.current && previewId.current) void api.cancelCleanup(host.id, previewId.current).catch(() => {});
  }, [host.id]);

  const left = preview ? secondsUntil(preview.expires_at, now) : 0;
  const expired = phase === "plan" && preview !== null && left === 0;
  const nodes = preview ? [...preview.plan.nodes].sort((a, b) => Number(b.role === "BACKUP") - Number(a.role === "BACKUP")) : [];
  const clustered = nodes.length > 1;
  const window = preview ? duration(t, preview.window_seconds) : "";
  const nameOk = confirmName === host.name;

  async function deploy() {
    if (!preview || !nameOk || notAdmin) return;
    setPhase("deploying");
    setError(null);
    try {
      spent.current = true;
      const operation = await api.deployCleanup(host.id, preview.preview_id, confirmName);
      toast.show(t(operation.status === "awaiting_confirm" ? "cleanup.toast.deployedConfirm" : operation.status === "done" ? "cleanup.toast.deployedDone" : "cleanup.toast.deployedCheck"),
        operation.status === "awaiting_confirm" || operation.status === "done" ? "success" : "info");
      onDeployed(operation);
    } catch (e) {
      // The backend consumes the preview at the first moment it is looked at: a failed roll-out needs a new one.
      setError(errorMessage(e, "cleanup.deploy.failed"));
      toast.show(errorMessage(e, "cleanup.deploy.failed"), "danger");
      setPreview(null);
      setPhase("plan");
    }
  }

  const title = t(`cleanup.preview.title.${request.kind}`);
  const hasPlan = phase === "plan" && preview !== null;
  const deployDisabledReason = notAdmin ? t("cleanup.preview.deployNotAdmin") : expired ? t("cleanup.preview.expired") : !nameOk ? t("cleanup.preview.deployDisabledName") : null;

  return (
    <>
      <Modal title={title} wide onClose={() => { if (phase !== "deploying" && !history) onClose(); }}>
        <div className="space-y-4">
          <p className="text-sm text-slate-600">
            {t(`cleanup.preview.intro.${request.kind}`, { host: host.name })}
            <InfoTip text={t(`cleanup.preview.introHint.${request.kind}`)} size={13} className="ml-1 align-middle" />
          </p>

          {phase === "ack" && (
            <div className="space-y-3">
              <p className="text-sm font-medium text-slate-800">{t(request.kind === "enable" ? "cleanup.ack.enableTitle" : "cleanup.ack.title")}</p>
              <ul className="space-y-2">
                {askAck.map((rule) => (
                  <li key={rule.uuid} className="rounded-xl border border-slate-200 p-3">
                    <label className="flex items-start gap-2.5 text-sm">
                      <input
                        type="checkbox"
                        className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300"
                        checked={acked.includes(rule.uuid)}
                        onChange={(e) => setAcked((current) => e.target.checked ? [...current, rule.uuid] : current.filter((u) => u !== rule.uuid))}
                      />
                      <span className="min-w-0">
                        <span className="block break-words font-medium text-slate-900">{rule.label}</span>
                        <span className="block text-slate-600">{t(request.kind === "enable" ? "cleanup.ack.enableText" : "cleanup.ack.text", { rule: rule.label })}</span>
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => void compute(acked)} disabled={!allAcked}>{t("cleanup.preview.compute")}</Button>
                <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
              </div>
            </div>
          )}

          {phase === "loading" && <Loading>{t("cleanup.preview.loading")}</Loading>}

          {error && <p role="alert" className="break-words rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}

          {phase === "plan" && !preview && (
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void compute(acked)}><RefreshCw size={14} aria-hidden="true" />{t("cleanup.preview.again")}</Button>
              <Button variant="ghost" onClick={onClose}>{t("common.close")}</Button>
            </div>
          )}

          {(hasPlan || phase === "deploying") && preview && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
                <span>{expired
                  ? <span role="alert" className="font-medium text-red-600">{t("cleanup.preview.expired")}</span>
                  : t("cleanup.preview.validFor", { time: formatClock(left) })}</span>
                {clustered && <span>{t("cleanup.preview.clusterOrder")}</span>}
              </div>
              {nodes.map((node, index) => (
                <section key={node.host_id} aria-label={node.host_name} className="space-y-2">
                  <h3 className="flex flex-wrap items-center gap-2 text-sm font-semibold text-slate-800">
                    {clustered && <span className="text-slate-400">{index + 1}.</span>}
                    {node.host_name}
                    {node.role && <Badge tone={node.role === "MASTER" ? "info" : "neutral"}>{node.role}</Badge>}
                  </h3>
                  {node.opnexus_source?.status && (
                    <p className={node.opnexus_source.status === "unknown" ? "text-xs text-amber-700" : "text-xs text-slate-500"}>
                      {t(`cleanup.preview.opnexusSource.${node.opnexus_source.status}`, { address: node.opnexus_source.address ?? "" })}
                    </p>
                  )}
                  <ul className="space-y-2">
                    {node.items.map((item) => (
                      <PlanItem
                        key={item.uuid}
                        item={item}
                        kind={preview.kind}
                        sources={preview.plan.sources ?? []}
                        coverName={coverName}
                        acknowledged={preview.plan.acknowledged.includes(item.given)}
                        onHistory={() => { setHistory(item); setHistoryNode(node); }}
                      />
                    ))}
                  </ul>
                </section>
              ))}

              <div className="space-y-3 border-t border-slate-200 pt-4">
                {request.kind !== "delete" && (
                  <p className="flex items-start gap-1.5 rounded-lg bg-flame-50 p-3 text-sm text-flame-800">
                    <TriangleAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <span>{t("cleanup.preview.autoRevert", { window })}</span>
                  </p>
                )}
                {!notAdmin && (
                  <label className="block text-sm">
                    <span className="mb-1 flex items-center gap-1 font-medium text-slate-700">{t("cleanup.preview.confirmName")}<InfoTip text={t("cleanup.preview.confirmNameHint")} size={13} /></span>
                    <Input
                      autoComplete="off"
                      spellCheck={false}
                      aria-label={t("cleanup.preview.confirmNameAria", { host: host.name })}
                      aria-describedby={confirmName && !nameOk ? nameErrorId : undefined}
                      value={confirmName}
                      placeholder={host.name}
                      disabled={phase === "deploying"}
                      onChange={(e) => setConfirmName(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && nameOk && !expired && phase === "plan") void deploy(); }}
                    />
                    {confirmName && !nameOk && <span id={nameErrorId} className="mt-1 block text-xs text-slate-500">{t("cleanup.preview.deployDisabledName")}</span>}
                  </label>
                )}
                <div className="flex flex-wrap items-center gap-2">
                  <Tooltip content={deployDisabledReason} focusable={deployDisabledReason !== null}>
                    <Button
                      variant={request.kind === "delete" ? "danger" : "primary"}
                      onClick={() => void deploy()}
                      disabled={deployDisabledReason !== null || phase === "deploying"}
                      loading={phase === "deploying"}
                    >
                      {t(`cleanup.deploy.${request.kind}`)}
                    </Button>
                  </Tooltip>
                  {expired && <Button variant="outline" onClick={() => void compute(acked)}><RefreshCw size={14} aria-hidden="true" />{t("cleanup.preview.again")}</Button>}
                  <Button variant="ghost" onClick={onClose} disabled={phase === "deploying"}>{t("common.cancel")}</Button>
                  {notAdmin && <InfoTip text={t("cleanup.preview.deployNotAdmin")} size={15} />}
                </div>
              </div>
            </div>
          )}
        </div>
      </Modal>
      {history && historyNode && (
        <RuleHistoryModal
          title={history.description}
          targets={[{ hostId: historyNode.host_id, hostName: historyNode.host_name, uuid: history.uuid }]}
          onClose={() => { setHistory(null); setHistoryNode(null); }}
        />
      )}
    </>
  );
}
