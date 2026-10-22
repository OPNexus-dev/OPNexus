"use client";

import { ConfidenceBadge, KIND_TONE } from "@/components/CleanupBadges";
import { CleanupPreviewModal, CleanupRequest } from "@/components/CleanupPreviewModal";
import { CleanupRestoreModal } from "@/components/CleanupRestoreModal";
import { CleanupSettings } from "@/components/CleanupSettings";
import { OperationBanner, OperationHistory } from "@/components/CleanupOperations";
import { useLanguage } from "@/components/LanguageProvider";
import { RuleHistoryModal } from "@/components/RuleHistoryModal";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Collapsible } from "@/components/ui/Collapsible";
import { EmptyState } from "@/components/ui/EmptyState";
import { FieldGroup, Select } from "@/components/ui/Field";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { api, CleanupActionKind, CleanupCandidate, CleanupCategory, CleanupItem, CleanupKind, CleanupOperation, GovernanceSettings, Host, RuleCleanupResult } from "@/lib/api";
import { activeOperation, historyOperations, isOpen, selectability } from "@/lib/cleanup";
import { useNotAdmin, useReadOnly } from "@/lib/useReadOnly";
import { History, Info, ListChecks, RefreshCw, Settings } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const THRESHOLDS = [7, 30, 90, 180];
const KINDS: CleanupKind[] = ["unused", "shadowed", "duplicate", "disabled_long"];

function CategoryLine({ category, days }: { category: CleanupCategory; days: number }) {
  const { t, locale } = useLanguage();
  let detail: string | null = null;
  if (category.kind === "unused") detail = t("cleanup.detail.unused", { days });
  else if (category.kind === "disabled_long") {
    detail = category.since ? t("cleanup.detail.disabledSince", { when: new Date(category.since).toLocaleDateString(locale) }) : t("cleanup.detail.disabled", { days });
  } else if (category.by) {
    detail = t(category.by.automatic ? "cleanup.detail.byAutomatic" : "cleanup.detail.by", { rule: category.by.description || category.by.uuid || "?" });
  }
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <Tooltip content={t(`cleanup.category.${category.kind}_hint`)} focusable>
        <Badge tone={KIND_TONE[category.kind]}>{t(`cleanup.category.${category.kind}`)}</Badge>
      </Tooltip>
      <ConfidenceBadge confidence={category.confidence} />
      {detail && <span className="min-w-0 break-words text-xs text-slate-500">{detail}</span>}
    </div>
  );
}

/** What the row offers: the action a selection would run (and whether it needs the rule's own acknowledgement), or why not. */
function ActionCell({ candidate, unusedDays }: { candidate: CleanupCandidate; unusedDays: number }) {
  const { t } = useLanguage();
  const sel = selectability(candidate, unusedDays);
  if (sel.mode) {
    return (
      <div className="flex flex-col items-start gap-1">
        <Badge tone={sel.mode === "delete" ? "danger" : "neutral"}>{t(`cleanup.action.${sel.mode}`)}</Badge>
        {sel.mode === "disable" && sel.needsAck && (
          <Tooltip content={t("cleanup.select.needsAckHint")} focusable><Badge tone="warning">{t("cleanup.select.needsAck")}</Badge></Tooltip>
        )}
      </div>
    );
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <Tooltip content={t(`cleanup.notRemovable.${sel.reason}_hint`)} focusable>
        <Badge tone="neutral">{t(`cleanup.notRemovable.${sel.reason}`)}</Badge>
      </Tooltip>
      {sel.reason === "managed" && (
        <Link href="/rules" className="text-xs text-brand-600 hover:underline">
          {t("cleanup.notRemovable.managedLink", { rule: candidate.managed_by?.name ?? "" })}
        </Link>
      )}
    </div>
  );
}

const POLL_MS = 5000;

export function RuleCleanup() {
  const { t, errorMessage, locale } = useLanguage();
  const toast = useToast();
  const readOnly = useReadOnly();
  const notAdmin = useNotAdmin();
  const router = useRouter();
  const params = useSearchParams();
  const [hosts, setHosts] = useState<Host[] | null>(null);
  const [result, setResult] = useState<RuleCleanupResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [refreshNext, setRefreshNext] = useState(false);
  const [history, setHistory] = useState<CleanupCandidate | null>(null);
  const [governance, setGovernance] = useState<GovernanceSettings | null>(null);
  const [operations, setOperations] = useState<CleanupOperation[]>([]);
  const [opsLoaded, setOpsLoaded] = useState(false);
  const [selection, setSelection] = useState<{ kind: CleanupActionKind | null; uuids: string[] }>({ kind: null, uuids: [] });
  const [request, setRequest] = useState<CleanupRequest | null>(null);
  const [restoring, setRestoring] = useState<CleanupItem | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const bannerRef = useRef<HTMLElement>(null);
  const focusBanner = useRef(false);
  const lastActive = useRef<string | null>(null);

  const enabledHosts = useMemo(() => (hosts ?? []).filter((h) => h.enabled), [hosts]);
  const days = THRESHOLDS.includes(Number(params.get("days"))) ? Number(params.get("days")) : 30;
  const hostId = params.get("host") && enabledHosts.some((h) => h.id === params.get("host")) ? (params.get("host") as string) : enabledHosts[0]?.id ?? "";
  const host = enabledHosts.find((h) => h.id === hostId) ?? null;
  const active = activeOperation(operations);
  const past = useMemo(() => historyOperations(operations), [operations]);
  const configured = governance?.cleanup_management_sources_configured ?? true;
  const maxRules = governance?.cleanup_max_rules ?? 10;

  useEffect(() => {
    api.listHosts().then(setHosts).catch((e) => { setHosts([]); setError(errorMessage(e, "cleanup.loadFailed")); });
    api.getGovernance().then(setGovernance).catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!hostId) { setResult(null); return; }
    let live = true;
    setLoading(true);
    setError(null);
    api.getRuleCleanup(hostId, days, refreshNext)
      .then((r) => live && setResult(r))
      .catch((e) => { if (live) { setResult(null); setError(errorMessage(e, "cleanup.loadFailed")); } })
      .finally(() => { if (live) { setLoading(false); setRefreshNext(false); } });
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId, days, tick]);

  useEffect(() => { setSelection({ kind: null, uuids: [] }); }, [result]);

  const reloadCandidates = useCallback(() => { setRefreshNext(true); setTick((n) => n + 1); }, []);

  const loadOperations = useCallback(async () => {
    if (!hostId) { setOperations([]); setOpsLoaded(true); return; }
    try {
      const list = await api.listCleanupOperations(hostId);
      setNow(Date.now());
      setOperations(list);
      // Somebody else (or the automatic revert) resolved the operation that was open a moment ago.
      const open = activeOperation(list);
      const before = lastActive.current;
      if (before && before !== open?.id) {
        const ended = list.find((op) => op.id === before);
        if (ended && !isOpen(ended)) {
          toast.show(t(`cleanup.toast.ended.${ended.status}`), ended.status === "reverted" ? "info" : "success");
          reloadCandidates();
        }
      }
      lastActive.current = open?.id ?? null;
    } catch (e) {
      setError(errorMessage(e, "cleanup.loadFailed"));
    } finally {
      setOpsLoaded(true);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId]);

  useEffect(() => { setOpsLoaded(false); lastActive.current = null; void loadOperations(); }, [loadOperations]);

  // While something is open: poll, and tick the countdown once a second.
  const activeId = active?.id;
  useEffect(() => {
    if (!activeId) return;
    const poll = setInterval(() => void loadOperations(), POLL_MS);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(poll); clearInterval(clock); };
  }, [activeId, loadOperations]);

  useEffect(() => {
    if (activeId && focusBanner.current) { focusBanner.current = false; bannerRef.current?.focus(); }
  }, [activeId]);

  function update(next: { host?: string; days?: number }) {
    const q = new URLSearchParams();
    q.set("tab", "cleanup");
    q.set("host", next.host ?? hostId);
    q.set("days", String(next.days ?? days));
    router.replace(`/analysis?${q.toString()}`);
  }

  const selectedKind = selection.kind;
  function toggle(candidate: CleanupCandidate, mode: CleanupActionKind, on: boolean) {
    const id = candidate.uuid ?? "";
    setSelection((current) => {
      const uuids = on ? [...current.uuids.filter((u) => u !== id), id] : current.uuids.filter((u) => u !== id);
      return { kind: uuids.length ? mode : null, uuids };
    });
  }

  function openPreview() {
    if (!result || !selectedKind) return;
    const rules = selection.uuids.flatMap((uuid) => {
      const candidate = result.candidates.find((c) => c.uuid === uuid);
      if (!candidate) return [];
      const sel = selectability(candidate, result.unused_days);
      return [{ uuid, label: candidate.description || uuid, needsAck: sel.mode === "disable" && sel.needsAck }];
    });
    setRequest({ kind: selectedKind, rules });
  }

  function coverName(uuid: string): string | null {
    for (const candidate of result?.candidates ?? []) {
      const by = candidate.categories.find((c) => c.by?.uuid === uuid)?.by;
      if (by) return by.description || by.uuid;
    }
    return null;
  }

  async function act(name: "confirm" | "revert" | "retry") {
    if (!active) return;
    setBusy(name);
    try {
      const fn = name === "confirm" ? api.confirmCleanup : name === "revert" ? api.revertCleanup : api.retryCleanup;
      // the operation's own anchor host: in a cluster it may be the other node than the one shown
      const op = await fn(active.host_id, active.id);
      const specific = t(`cleanup.toast.${name}.${op.status}`);
      toast.show(specific === `cleanup.toast.${name}.${op.status}` ? t(`cleanup.toast.${name}.done`) : specific,
        op.status === "revert_failed" || op.status === "failed" ? "danger" : "success");
      lastActive.current = null;
      await loadOperations();
      reloadCandidates();
    } catch (e) {
      toast.show(errorMessage(e, `cleanup.${name}Failed`), "danger");
      await loadOperations();
    } finally {
      setBusy(null);
    }
  }

  function followUp(kind: "enable" | "delete", item: CleanupItem, uuid: string) {
    setRequest({ kind, rules: [{ uuid, label: item.description || uuid, needsAck: false }] });
  }

  const total = result ? KINDS.reduce((sum, kind) => sum + result.counts[kind], 0) : 0;
  const locked = !configured;
  const limitReached = selection.uuids.length >= maxRules;
  const previewBlockedReason = locked ? t("cleanup.select.locked") : active ? t("cleanup.select.busy") : selection.uuids.length === 0 ? t("cleanup.select.none") : null;
  const checkboxLabel = (c: CleanupCandidate) => t("cleanup.select.row", { rule: c.description || c.uuid || "" });

  return (
    <div>
      <Card className="mb-6 p-4 sm:p-5">
        <form onSubmit={(e) => e.preventDefault()} aria-label={t("cleanup.form.title")} className="flex flex-wrap items-end gap-4">
          <div className="w-full sm:w-64">
            <FieldGroup label={t("cleanup.form.host")}>
              <Select value={hostId} onChange={(e) => update({ host: e.target.value })} disabled={enabledHosts.length === 0}>
                {enabledHosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
              </Select>
            </FieldGroup>
          </div>
          <div className="w-full sm:w-48">
            <FieldGroup label={t("cleanup.form.threshold")} hint={t("cleanup.form.thresholdHint")}>
              <Select value={String(days)} onChange={(e) => update({ days: Number(e.target.value) })}>
                {THRESHOLDS.map((n) => <option key={n} value={n}>{t("cleanup.form.days", { n })}</option>)}
              </Select>
            </FieldGroup>
          </div>
          <Button type="button" variant="outline" onClick={() => { reloadCandidates(); void loadOperations(); }} disabled={loading || !hostId} title={t("cleanup.form.refreshHint")}>
            <RefreshCw size={14} aria-hidden="true" />
            {t("common.refresh")}
          </Button>
        </form>
      </Card>

      {locked && (
        <Card className="mb-6 p-4 sm:p-5">
          <div className="flex items-start gap-3">
            <Info size={18} className="mt-0.5 shrink-0 text-slate-400" aria-hidden="true" />
            <div className="min-w-0 flex-1 space-y-3">
              <div>
                <p className="text-sm font-medium text-slate-800">{t("cleanup.mgmt.title")}</p>
                <p className="mt-0.5 text-sm text-slate-600">
                  {t("cleanup.mgmt.text")}
                  <InfoTip text={t("cleanup.mgmt.hint")} size={13} className="ml-1 align-middle" />
                </p>
              </div>
              {notAdmin ? <p className="text-sm text-slate-500">{t("cleanup.mgmt.askAdmin")}</p> : <CleanupSettings compact onSaved={setGovernance} />}
            </div>
          </div>
        </Card>
      )}

      {active && host && (
        <OperationBanner
          ref={bannerRef}
          operation={active}
          now={now}
          busy={busy}
          readOnly={readOnly}
          notAdmin={notAdmin}
          operatorsMayDeploy={governance?.operators_may_deploy ?? true}
          onConfirm={() => void act("confirm")}
          onRevert={() => void act("revert")}
          onRetry={() => void act("retry")}
        />
      )}

      {error && <p role="alert" className="mb-4 text-sm text-red-600">{error}</p>}
      {hosts !== null && enabledHosts.length === 0 && <EmptyState icon={ListChecks} title={t("cleanup.noHosts")} />}
      {loading && !result && <Skeleton className="h-40 w-full" />}

      {result && (
        <div aria-busy={loading}>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {KINDS.map((kind) => (
              <Tooltip key={kind} content={t(`cleanup.category.${kind}_hint`)} focusable>
                <Badge tone={result.counts[kind] > 0 ? KIND_TONE[kind] : "neutral"}>{t(`cleanup.category.${kind}`)}: {result.counts[kind]}</Badge>
              </Tooltip>
            ))}
            <span className="text-xs text-slate-500">{t("cleanup.rulesChecked", { count: result.rules_total })}</span>
            <InfoTip
              text={result.usage.watching_since
                ? t("cleanup.watching", { when: new Date(result.usage.watching_since).toLocaleDateString(locale), collecting: result.usage.collecting })
                : t("cleanup.notWatching")}
              size={14}
            />
            {!notAdmin && !locked && (
              <Link href="/settings#cleanup" className="ml-auto inline-flex items-center gap-1 text-xs text-brand-600 hover:underline">
                <Settings size={13} aria-hidden="true" />{t("cleanup.settings.link")}
              </Link>
            )}
          </div>
          {result.notes.length > 0 && (
            <div className="mb-3 flex flex-wrap gap-1.5">
              {result.notes.map((code) => {
                const label = t(`cleanup.notes.${code}`);
                return (
                  <Tooltip key={code} content={label === `cleanup.notes.${code}` ? code : t(`cleanup.notes.${code}_hint`)} focusable>
                    <Badge tone="warning">{label === `cleanup.notes.${code}` ? code : label}</Badge>
                  </Tooltip>
                );
              })}
            </div>
          )}

          {total === 0 ? (
            <EmptyState icon={ListChecks} title={t("cleanup.empty.title")} description={t("cleanup.empty.description", { days })} />
          ) : (
            <>
              {!readOnly && (
                <div className="mb-3 flex flex-wrap items-center gap-3">
                  <p role="status" aria-live="polite" className="text-sm text-slate-600">{t("cleanup.select.count", { n: selection.uuids.length, max: maxRules })}</p>
                  <Tooltip content={previewBlockedReason} focusable={previewBlockedReason !== null}>
                    <Button onClick={openPreview} disabled={previewBlockedReason !== null}>
                      <ListChecks size={15} aria-hidden="true" />
                      {t(selectedKind === "delete" ? "cleanup.select.previewDelete" : "cleanup.select.previewDisable")}
                    </Button>
                  </Tooltip>
                  {selection.uuids.length > 0 && <Button variant="ghost" onClick={() => setSelection({ kind: null, uuids: [] })}>{t("cleanup.select.clear")}</Button>}
                </div>
              )}
              <Card className="overflow-x-auto p-0">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">{t("cleanup.tableCaption", { host: result.host_name })}</caption>
                  <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      {!readOnly && <th scope="col" className="w-10 px-4 py-3"><span className="sr-only">{t("cleanup.select.column")}</span></th>}
                      <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.rule")}</th>
                      <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.finding")}</th>
                      <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.lastHit")}</th>
                      <th scope="col" className="px-4 py-3 font-medium">{t("cleanup.col.action")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.candidates.map((candidate) => {
                      const sel = selectability(candidate, result.unused_days);
                      const id = candidate.uuid ?? "";
                      const checked = selection.uuids.includes(id);
                      const reason = sel.mode === null ? t(`cleanup.notRemovable.${sel.reason}_hint`)
                        : locked ? t("cleanup.select.locked")
                        : active ? t("cleanup.select.busy")
                        : selectedKind && selectedKind !== sel.mode ? t("cleanup.select.otherKind")
                        : !checked && limitReached ? t("cleanup.select.limit", { max: maxRules })
                        : null;
                      return (
                        <tr key={candidate.uuid ?? candidate.label ?? candidate.description} className={`border-b border-slate-100 align-top last:border-0 ${checked ? "bg-brand-50/40" : ""}`}>
                          {!readOnly && (
                            <td className="px-4 py-3">
                              <Tooltip content={reason} focusable={reason !== null}>
                                <input
                                  type="checkbox"
                                  className="h-4 w-4 rounded border-slate-300"
                                  checked={checked}
                                  disabled={reason !== null && !checked}
                                  aria-label={checkboxLabel(candidate)}
                                  onChange={(e) => sel.mode && toggle(candidate, sel.mode, e.target.checked)}
                                />
                              </Tooltip>
                            </td>
                          )}
                          <td className="max-w-[18rem] px-4 py-3">
                            <div className="flex items-start gap-1.5">
                              <span className="min-w-0 break-words font-medium text-slate-900">{candidate.description || candidate.uuid}</span>
                              <button
                                type="button"
                                onClick={() => setHistory(candidate)}
                                className="shrink-0 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
                                aria-label={t("cleanup.history", { rule: candidate.description || candidate.uuid || "" })}
                                title={t("cleanup.historyTitle")}
                              >
                                <History size={15} aria-hidden="true" />
                              </button>
                            </div>
                            <p className="mt-0.5 break-words text-xs text-slate-500">
                              {candidate.action ? t(`analysis.verdict.${candidate.action}`) : "?"} · {candidate.interfaces.join(", ") || t("cleanup.allInterfaces")} · {candidate.source} → {candidate.destination}
                              {candidate.destination_port ? `:${candidate.destination_port}` : ""}
                              {!candidate.enabled && <span className="ml-1">({t("cleanup.disabledRule")})</span>}
                            </p>
                          </td>
                          <td className="min-w-[16rem] px-4 py-3">
                            <div className="space-y-1.5">
                              {candidate.categories.map((category) => <CategoryLine key={category.kind} category={category} days={result.unused_days} />)}
                            </div>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-600">
                            {candidate.last_hit_at
                              ? new Date(candidate.last_hit_at).toLocaleString(locale)
                              : candidate.watched_since
                                ? <Tooltip content={t("cleanup.noHitSince", { when: new Date(candidate.watched_since).toLocaleDateString(locale) })} focusable><span>{t("cleanup.noHit")}</span></Tooltip>
                                : <span className="text-slate-300">–</span>}
                          </td>
                          <td className="min-w-[9rem] px-4 py-3"><ActionCell candidate={candidate} unusedDays={result.unused_days} /></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </Card>
            </>
          )}
        </div>
      )}

      {host && opsLoaded && (
        <Collapsible title={t("cleanup.hist.title")} count={past.length} className="mt-8">
          <OperationHistory
            operations={past}
            hostId={hostId}
            now={now}
            minDays={governance?.cleanup_delete_min_days ?? 7}
            readOnly={readOnly}
            notAdmin={notAdmin}
            busy={busy !== null || active !== null}
            onRestore={setRestoring}
            onFollowUp={followUp}
          />
        </Collapsible>
      )}

      {request && host && (
        <CleanupPreviewModal
          host={{ id: host.id, name: host.name }}
          request={request}
          unusedDays={result?.unused_days ?? days}
          coverName={coverName}
          onClose={() => setRequest(null)}
          onDeployed={(operation) => {
            setRequest(null);
            setSelection({ kind: null, uuids: [] });
            if (isOpen(operation)) { focusBanner.current = true; lastActive.current = operation.id; }
            setOperations((current) => [operation, ...current.filter((op) => op.id !== operation.id)]);
            setNow(Date.now());
            void loadOperations();
            reloadCandidates();
          }}
        />
      )}
      {restoring && host && (
        <CleanupRestoreModal
          host={{ id: host.id, name: host.name }}
          item={restoring}
          onClose={() => setRestoring(null)}
          onRestored={() => { void loadOperations(); reloadCandidates(); }}
        />
      )}
      {history && result && (
        <RuleHistoryModal
          title={history.description || history.uuid || ""}
          targets={[{ hostId: result.host_id, hostName: result.host_name, uuid: history.uuid ?? "" }]}
          onClose={() => setHistory(null)}
        />
      )}
    </div>
  );
}
