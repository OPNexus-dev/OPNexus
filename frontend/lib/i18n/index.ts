import de from "./de.json";
import en from "./en.json";

export type Language = "de" | "en";
export type TranslationParams = Record<string, string | number>;

import { serverMessagesEn, serverWordsEn } from "./serverMessages";

const dictionaries: Record<Language, unknown> = { de, en };

// Long literal sentences may also occur joined into a larger string (e.g. several
// maintenance blockers concatenated into one API error), so they double as
// substring replacements when no whole-string entry matches.
const sentenceEntries = serverMessagesEn
  .filter((entry): entry is [string, string] => typeof entry[0] === "string" && entry[0].length >= 20)
  .sort((a, b) => b[0].length - a[0].length);

function lookupServerText(text: string, depth: number): string | null {
  if (!text) return null;
  for (const [pattern, template] of serverMessagesEn) {
    if (typeof pattern === "string") {
      if (pattern === text) return template;
      continue;
    }
    const match = pattern.exec(text);
    if (!match) continue;
    return template.replace(/\{(\w+)\}/g, (whole, name: string, offset: number) => {
      const raw = match.groups?.[name];
      if (raw === undefined) return whole;
      const value = depth < 3 ? translatePart(raw, depth + 1) : raw;
      // Only a translated noun (e.g. "rule") needs a capital at sentence start; captured host names keep their case.
      return offset === 0 && value !== raw ? value.charAt(0).toUpperCase() + value.slice(1) : value;
    });
  }
  return null;
}

function translatePart(text: string, depth: number): string {
  return lookupServerText(text, depth) ?? serverWordsEn[text] ?? text;
}

// The backend still emits some German prose inside successful responses (deploy
// previews, host messages, revision notes) and validation errors. German is the
// source text, so only English needs a lookup; unknown text passes through.
export function translateServerText(language: Language, text: string): string {
  if (language === "de" || !text) return text;
  const whole = lookupServerText(text, 0);
  if (whole !== null) return whole;
  let result = text;
  for (const [german, english] of sentenceEntries) {
    if (result.includes(german)) result = result.split(german).join(english);
  }
  return result;
}

export function translateServerParams(language: Language, params?: Record<string, string>): Record<string, string> | undefined {
  if (!params) return params;
  return Object.fromEntries(Object.entries(params).map(([key, value]) => [key, typeof value === "string" ? translateServerText(language, value) : value]));
}

// Errors whose parameter is a comma-separated list of machine codes (the server sends codes, never prose):
// the parameter and the catalog prefix that holds their texts.
const CODE_LIST_PARAMS: Record<string, { param: string; prefix: string }> = {
  OBJECT_NOT_ADOPTABLE: { param: "reasons", prefix: "adopt.reasons" },
  RULE_NOT_ADOPTABLE: { param: "reasons", prefix: "rules.adopt.reasons" },
  ADOPT_LINK_TARGET_CHANGED: { param: "fields", prefix: "adopt.link.fields" },
  GUI_GATEWAY_NOT_CONFIGURED: { param: "reasons", prefix: "guiAccess.reason" },
  NETWORK_CHANGE_BLOCKED: { param: "reasons", prefix: "network.blocks" },
  NETWORK_ACK_REQUIRED: { param: "reasons", prefix: "network.risks" },
  HOST_CONNECTION_FAILED: { param: "reason", prefix: "hosts.connection" },
  BEREINIGUNG_REGEL_NICHT_AENDERBAR: { param: "reason", prefix: "cleanup.refuse" },
  BEREINIGUNG_OFFENE_OPERATION: { param: "reason", prefix: "cleanup.openReason" },
  BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG: { param: "reason", prefix: "cleanup.clusterReason" },
  BEREINIGUNG_EINSTELLUNG_UNGUELTIG: { param: "reason", prefix: "cleanup.settingsReason" },
};

/** `reasons: "name_exists, proto_set"` -> readable sentences joined by " · "; an unknown code stays as it is.
 *  `reason_details` (JSON, code -> text) fills {detail}; placeholders nobody can fill are dropped. */
export function translateReasonParams(language: Language, code: string, params?: Record<string, string>): Record<string, string> | undefined {
  const spec = CODE_LIST_PARAMS[code];
  if (!spec || !params || typeof params[spec.param] !== "string") return params;
  let details: Record<string, string> = {};
  try { details = JSON.parse(params.reason_details || "{}") as Record<string, string>; } catch { /* plain codes only */ }
  const items = params[spec.param].split(",").map((r) => r.trim()).filter(Boolean).map((item) => {
    const key = `${spec.prefix}.${item}`;
    const text = translate(language, key, details[item] !== undefined ? { detail: details[item] } : {});
    if (text === key) return item;
    // A placeholder without a value goes away together with the quotes/colon around it.
    return text.replace(/\s*[„“"'(]?\{\w+\}[“”"')]?/g, "").replace(/[:\s]+$/, "").replace(/ {2,}/g, " ").trim();
  });
  return { ...params, [spec.param]: items.join(" · ") };
}

// Looks up `${prefix}.${key}`, falling back to the raw key for values the catalog does not know.
export function labelFor(t: (key: string) => string, prefix: string, key: string): string {
  const full = `${prefix}.${key}`;
  const value = t(full);
  return value === full ? key : value;
}

export function translate(language: Language, key: string, params: TranslationParams = {}): string {
  let value: unknown = dictionaries[language];
  for (const part of key.split(".")) {
    if (!value || typeof value !== "object" || !(part in value)) return key;
    value = (value as Record<string, unknown>)[part];
  }
  if (typeof value !== "string") return key;
  return value.replace(/\{([^}]+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
  );
}
