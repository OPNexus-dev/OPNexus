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
