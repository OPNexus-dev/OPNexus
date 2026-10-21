"use client";

import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Field";
import { Tooltip } from "@/components/ui/Tooltip";
import { useLanguage } from "@/components/LanguageProvider";
import { labelFor } from "@/lib/i18n";
import type { LinkDiff, LinkTarget } from "@/lib/api";
import { useState } from "react";

interface Props {
  linkable: boolean;
  matches: LinkTarget[];
  similar: LinkTarget[];
  /** The object could also be adopted as a new central object (false e.g. for an alias whose name is taken). */
  canAdoptNew: boolean;
  busy: boolean;
  anyBusy: boolean;
  onLink: (objectId: string) => void;
  onAdoptNew: () => void;
}

function shorten(value: string): string {
  const flat = value.replace(/\n/g, " ⏎ ");
  return flat.length > 120 ? `${flat.slice(0, 117)}…` : flat;
}

function showValue(value: string, t: (key: string) => string): string {
  if (value === "true") return t("adopt.link.yes");
  if (value === "false") return t("adopt.link.no");
  return value === "" ? "–" : shorten(value);
}

/**
 * What to do with an object found on a firewall when central objects already exist: link it to its exact
 * twin (the default, no duplicate), adopt it as an own object, or — when there is only a similar object —
 * see exactly which fields differ. Nothing is ever linked without the user's click, and the server
 * compares again with the live state before it links.
 */
export function AdoptLinkChoice({ linkable, matches, similar, canAdoptNew, busy, anyBusy, onLink, onAdoptNew }: Props) {
  const { t } = useLanguage();
  const [chosen, setChosen] = useState("");
  const selected = matches.find((m) => m.id === chosen) ?? matches[0];

  const fieldLabel = (diff: LinkDiff) => labelFor(t, "adopt.link.fields", diff.field);

  if (linkable && selected) {
    return (
      <div className="mt-2 space-y-2 rounded-lg border border-[rgb(var(--tone-info-border))] bg-[rgb(var(--tone-info-bg))] p-3 text-sm text-[rgb(var(--tone-info-text))]">
        <p>{t("adopt.link.found", { name: selected.name, hosts: selected.targets.join(", ") || "–" })}</p>
        {matches.length > 1 && (
          <label className="flex flex-wrap items-center gap-2 text-xs">
            {t("adopt.link.choose")}
            <Select className="w-auto" value={selected.id} onChange={(e) => setChosen(e.target.value)}>
              {matches.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.targets.join(", ") || "–"})
                </option>
              ))}
            </Select>
          </label>
        )}
        <div className="flex flex-wrap gap-2">
          <Tooltip content={t("adopt.link.linkTip")}>
            <Button onClick={() => onLink(selected.id)} disabled={anyBusy} loading={busy}>
              {t("adopt.link.action")}
            </Button>
          </Tooltip>
          <Tooltip content={canAdoptNew ? t("adopt.link.newTip") : t("adopt.link.newBlockedTip")} focusable={!canAdoptNew}>
            <Button variant="outline" onClick={onAdoptNew} disabled={anyBusy || !canAdoptNew}>
              {t("adopt.link.adoptNew")}
            </Button>
          </Tooltip>
        </div>
      </div>
    );
  }

  if (similar.length > 0) {
    return (
      <div className="mt-2 space-y-1 rounded-lg border border-[rgb(var(--tone-warning-border))] bg-[rgb(var(--tone-warning-bg))] p-3 text-xs text-[rgb(var(--tone-warning-text))]">
        {similar.map((s) => (
          <div key={s.id}>
            <p className="font-medium">{t("adopt.link.similar", { name: s.name, hosts: s.targets.join(", ") || "–" })}</p>
            <ul className="mt-1 space-y-0.5">
              {s.diff.map((d) => (
                <li key={d.field} className="break-words">
                  <span className="font-medium">{fieldLabel(d)}:</span>{" "}
                  {d.field === "variables"
                    ? t("adopt.link.variableMissing", { names: d.central })
                    : t("adopt.link.diffValues", { central: showValue(d.central, t), live: showValue(d.live, t) })}
                </li>
              ))}
            </ul>
          </div>
        ))}
        <p>{t("adopt.link.similarNext")}</p>
      </div>
    );
  }
  return null;
}
