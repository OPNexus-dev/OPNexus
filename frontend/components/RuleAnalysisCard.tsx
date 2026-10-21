"use client";

import { useLanguage } from "@/components/LanguageProvider";
import { Badge, BadgeTone } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Tooltip } from "@/components/ui/Tooltip";
import type { AnalysisAction, AnalysisConfidence, AnalysisLeg, AnalysisResult, AnalysisRule, AnalysisStep, AnalysisTri } from "@/lib/api";
import clsx from "clsx";
import { AlertTriangle, Check, ChevronDown, ChevronRight, CircleHelp, ExternalLink, X } from "lucide-react";
import Link from "next/link";
import { useId, useState } from "react";

const ACTION_TONE: Record<AnalysisAction, BadgeTone> = { pass: "success", block: "danger", reject: "warning" };
const CONFIDENCE_TONE: Record<AnalysisConfidence, BadgeTone> = { sure: "info", probable: "warning", unknown: "neutral" };

/** Text for an engine code; an unknown code is shown raw instead of breaking the card. */
function useCodeText() {
  const { t } = useLanguage();
  return (family: "notes" | "unknown" | "assumptions", code: string) => {
    const label = t(`analysis.${family}.${code}`);
    if (label === `analysis.${family}.${code}`) return { label: code, hint: t("analysis.unknownCode", { code }) };
    return { label, hint: t(`analysis.${family}.${code}_hint`) };
  };
}

function ConfidenceBadge({ confidence, unresolved, unknowns }: { confidence: AnalysisConfidence; unresolved?: boolean; unknowns: string[] }) {
  const { t } = useLanguage();
  const codeText = useCodeText();
  const reasons = unknowns.map((code) => codeText("unknown", code).label);
  const tip = (
    <span className="block">
      {unresolved ? t("analysis.confidence.tip.unresolved") : t(`analysis.confidence.tip.${confidence}`)}
      {reasons.length > 0 && <span className="mt-1 block">{reasons.join(", ")}</span>}
    </span>
  );
  return (
    <Tooltip content={tip} focusable>
      <Badge tone={CONFIDENCE_TONE[confidence]} className={clsx(confidence === "unknown" && "border-dashed")} aria-label={`${t("analysis.confidence.label")}: ${t(`analysis.confidence.${confidence}`)}`}>
        {confidence === "unknown" ? <CircleHelp size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />}
        {t(`analysis.confidence.${confidence}`)}
      </Badge>
    </Tooltip>
  );
}

function Chip({ family, code }: { family: "notes" | "unknown" | "assumptions"; code: string }) {
  const codeText = useCodeText();
  const { label, hint } = codeText(family, code);
  return (
    <Tooltip content={hint} focusable>
      <Badge tone={family === "unknown" ? "warning" : family === "assumptions" ? "neutral" : "info"}>{label}</Badge>
    </Tooltip>
  );
}

function ruleName(rule: AnalysisRule, t: (k: string, p?: Record<string, string | number>) => string) {
  const name = rule.description || rule.label || rule.uuid;
  if (name) return name;
  return rule.pf_numbers.length ? t("analysis.result.pfNumber", { number: rule.pf_numbers.join(", ") }) : "-";
}

function endpoint(address: string, port: string | null) {
  return port ? `${address}:${port}` : address;
}

function RuleBlock({ rule, host }: { rule: AnalysisRule; host: string }) {
  const { t } = useLanguage();
  const details = [
    [t("analysis.result.ruleSource"), endpoint(rule.source, rule.source_port)],
    [t("analysis.result.ruleDestination"), endpoint(rule.destination, rule.destination_port)],
    rule.gateway ? [t("analysis.result.ruleGateway"), rule.gateway] : null,
    rule.schedule ? [t("analysis.result.ruleSchedule"), rule.schedule] : null,
  ].filter(Boolean) as string[][];
  const id = rule.uuid || (rule.pf_numbers.length ? `@${rule.pf_numbers[0]}` : "-");
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm" data-testid="decisive-rule">
      <p className="break-words font-medium text-slate-900">{ruleName(rule, t)}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Badge tone="neutral">{t(`analysis.section.${rule.section}`) === `analysis.section.${rule.section}` ? rule.section : t(`analysis.section.${rule.section}`)}</Badge>
        {rule.pf_numbers.length > 0 && <Badge tone="neutral">{t("analysis.result.pfNumber", { number: rule.pf_numbers.join(", ") })}</Badge>}
        {rule.quick && <Tooltip content={t("analysis.result.quickHint")} focusable><Badge tone="accent">{t("analysis.result.quick")}</Badge></Tooltip>}
        {rule.automatic && <Badge tone="neutral">{t("analysis.result.automatic")}</Badge>}
        {rule.legacy && <Badge tone="neutral">{t("analysis.result.legacy")}</Badge>}
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-slate-700">
        {details.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-slate-500">{key}</dt>
            <dd className="break-words font-mono text-xs leading-5">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-2">
        {rule.editable ? (
          <Tooltip content={t("analysis.result.rulesLinkHint", { id, host })}>
            <Link href="/rules?tab=live" className="inline-flex items-center gap-1 text-sm font-medium text-brand-600 underline">
              <ExternalLink size={14} aria-hidden="true" />
              {t("analysis.result.rulesLink")}
            </Link>
          </Tooltip>
        ) : (
          <span className="text-xs text-slate-500">{t("analysis.result.noLink")}</span>
        )}
      </div>
    </div>
  );
}

function TriIcon({ value, label }: { value: AnalysisTri; label: string }) {
  const { t } = useLanguage();
  const text = `${label}: ${t(`analysis.trace.result.${value}`)}`;
  const Icon = value === "true" ? Check : value === "false" ? X : CircleHelp;
  const tone = value === "true" ? "text-emerald-700" : value === "false" ? "text-slate-400" : "text-amber-700";
  return (
    <span className={clsx("inline-flex items-center gap-0.5 rounded border border-slate-200 px-1.5 py-0.5 text-xs", tone)}>
      <Icon size={12} aria-hidden="true" />
      <span className="text-slate-700">{label}</span>
      <span className="sr-only">{text}</span>
    </span>
  );
}

const EFFECT_TONE: Record<AnalysisStep["effect"], BadgeTone> = {
  decisive: "success", overridden: "neutral", possible: "warning", no_match: "neutral", not_loaded: "neutral", disabled: "neutral",
};

function StepRow({ step }: { step: AnalysisStep }) {
  const { t } = useLanguage();
  const struck = step.effect === "overridden";
  const fieldLabel = (key: string) => (t(`analysis.trace.field.${key}`) === `analysis.trace.field.${key}` ? key : t(`analysis.trace.field.${key}`));
  return (
    <li className="rounded-lg border border-slate-200 p-2 text-sm" data-effect={step.effect}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={EFFECT_TONE[step.effect]} className={clsx(step.effect === "possible" && "border-dashed")}>{t(`analysis.trace.effect.${step.effect}`)}</Badge>
        <span className={clsx("min-w-0 break-words font-medium text-slate-900", struck && "text-slate-500 line-through")}>{ruleName(step.rule, t)}</span>
        {step.rule.pf_numbers.length > 0 && <span className="text-xs text-slate-500">@{step.rule.pf_numbers.join(",")}</span>}
        {step.rule.quick && <Badge tone="accent">{t("analysis.result.quick")}</Badge>}
      </div>
      <p className={clsx("mt-0.5 break-words font-mono text-xs text-slate-500", struck && "line-through")}>
        {endpoint(step.rule.source, step.rule.source_port)} → {endpoint(step.rule.destination, step.rule.destination_port)}
        {step.rule.action ? ` (${step.rule.action})` : ""}
      </p>
      {Object.keys(step.fields).length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {Object.entries(step.fields).map(([key, value]) => <TriIcon key={key} value={value} label={fieldLabel(key)} />)}
        </div>
      )}
      {step.reasons.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1" aria-label={t("analysis.trace.reasonsTitle")}>
          {step.reasons.map((code) => <Chip key={code} family="unknown" code={code} />)}
        </div>
      )}
    </li>
  );
}

function Trace({ result }: { result: AnalysisResult }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const panelId = useId();
  const trace = result.trace ?? [];
  if (trace.length === 0) return null;
  return (
    <div className="mt-3">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        className="inline-flex items-center gap-1 rounded-lg text-sm font-medium text-slate-700 hover:text-brand-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-500"
      >
        {open ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
        {t("analysis.trace.title")}
      </button>
      {open && (
        <div id={panelId} className="mt-2">
          <p className="mb-2 text-xs text-slate-500">{t("analysis.trace.hint")}</p>
          {trace.map((leg) => {
            const steps = all ? leg.steps : leg.steps.filter((s) => s.effect !== "no_match" && s.effect !== "disabled");
            return (
              <section key={leg.leg} className="mb-3">
                <h4 className="mb-1.5 text-sm font-semibold text-slate-800">
                  {t("analysis.trace.leg", { direction: t(leg.direction === "out" ? "analysis.trace.dirOut" : "analysis.trace.dirIn"), interface: leg.interface })}
                </h4>
                {steps.length === 0 ? <p className="text-sm text-slate-500">{t("analysis.trace.noSteps")}</p> : (
                  <ol className="flex flex-col gap-1.5">
                    {steps.map((step, index) => <StepRow key={`${step.rule.uuid ?? step.rule.label ?? "rule"}-${index}`} step={step} />)}
                  </ol>
                )}
              </section>
            );
          })}
          {trace.some((leg) => leg.steps.some((s) => s.effect === "no_match" || s.effect === "disabled")) && (
            <label className="inline-flex items-center gap-2 text-sm text-slate-700">
              <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
              {t("analysis.trace.showAll", { count: trace.reduce((n, leg) => n + leg.steps.filter((s) => s.effect === "no_match" || s.effect === "disabled").length, 0) })}
            </label>
          )}
        </div>
      )}
    </div>
  );
}

function LegLine({ leg }: { leg: AnalysisLeg }) {
  const { t } = useLanguage();
  const label = t(leg.direction === "out" ? "analysis.result.legOut" : "analysis.result.legIn", { interface: leg.interface });
  const decided = leg.confidence !== "unknown" && leg.action;
  return (
    <li className="flex flex-wrap items-center gap-1.5 text-sm text-slate-700">
      <span>{label}</span>
      {leg.interface_source === "guessed" && <span className="text-xs text-slate-500">({t("analysis.result.guessed")})</span>}
      {decided ? (
        <Badge tone={ACTION_TONE[leg.action as AnalysisAction]}>{t(`analysis.verdict.${leg.action}`)}</Badge>
      ) : (
        <Badge tone="neutral" className="border-dashed">{t("analysis.verdict.unknown")}</Badge>
      )}
    </li>
  );
}

export function RuleAnalysisCard({ result }: { result: AnalysisResult }) {
  const { t, errorMessage } = useLanguage();
  const confidence = result.confidence ?? "unknown";
  const unresolved = !result.error && !result.action;
  const decided = !result.error && !unresolved && confidence !== "unknown" && result.action;
  const notes = result.notes ?? [];
  const unknowns = result.unknowns ?? [];
  const assumptions = result.query?.assumptions ?? [];
  const legs = result.legs ?? [];
  const showLegs = legs.length > 1 || legs.some((leg) => leg.interface_source === "guessed");

  return (
    <Card className={clsx("p-4", !decided && "border-dashed")} data-testid="analysis-card" data-state={result.error ? "error" : decided ? "decided" : "undecided"}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="break-words text-lg font-semibold text-slate-900">{result.host_name}</h3>
          {result.cluster_name && <p className="text-xs text-slate-500">{result.cluster_name}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {result.error ? (
            <Badge tone="danger"><AlertTriangle size={12} aria-hidden="true" />{t("analysis.result.hostError")}</Badge>
          ) : decided ? (
            <>
              <Badge tone={ACTION_TONE[result.action as AnalysisAction]} className="px-3 py-1 text-sm" data-testid="verdict">{t(`analysis.verdict.${result.action}`)}</Badge>
              <ConfidenceBadge confidence={confidence} unknowns={unknowns} />
            </>
          ) : (
            <>
              <Badge tone="neutral" className="border-dashed px-3 py-1 text-sm" data-testid="verdict">{unresolved ? t("analysis.result.unresolved") : t("analysis.result.undecided")}</Badge>
              <ConfidenceBadge confidence="unknown" unresolved={unresolved} unknowns={unknowns} />
            </>
          )}
        </div>
      </div>

      {result.error && <p role="alert" className="mt-2 text-sm text-red-600">{errorMessage(result.error, "analysis.queryFailed")}</p>}

      {!result.error && unresolved && <p className="mt-2 text-sm text-slate-600">{t("analysis.result.unresolvedHint")}</p>}
      {!result.error && !unresolved && !decided && <p className="mt-2 text-sm text-slate-600">{t("analysis.result.undecidedHint")}</p>}

      {!result.error && !unresolved && (
        <div className="mt-3">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
            {decided ? t("analysis.result.decisive") : t("analysis.result.decisiveIfKnown")}
            {!decided && result.action && <span className="ml-1 normal-case tracking-normal">({t(`analysis.verdict.${result.action}`)})</span>}
          </p>
          {result.decisive_rule ? (
            <RuleBlock rule={result.decisive_rule} host={result.host_name} />
          ) : (
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm">
              <p className="font-medium text-slate-900">{t("analysis.result.noRule")}</p>
              <p className="text-slate-600">{t("analysis.result.noRuleHint")}</p>
            </div>
          )}
        </div>
      )}

      {showLegs && <ul className="mt-3 flex flex-col gap-1">{legs.map((leg, index) => <LegLine key={index} leg={leg} />)}</ul>}

      {unknowns.length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">{t("analysis.result.openPoints")}</p>
          <div className="flex flex-wrap gap-1.5">{unknowns.map((code) => <Chip key={code} family="unknown" code={code} />)}</div>
        </div>
      )}
      {(notes.length > 0 || assumptions.length > 0) && (
        <div className="mt-3">
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">{t("analysis.result.hints")}</p>
          <div className="flex flex-wrap gap-1.5">
            {notes.map((code) => <Chip key={`n-${code}`} family="notes" code={code} />)}
            {assumptions.map((code) => <Chip key={`a-${code}`} family="assumptions" code={code} />)}
          </div>
        </div>
      )}

      {!result.error && <Trace result={result} />}
    </Card>
  );
}
