import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// jsdom does not apply media queries from a stylesheet, so this guards the rule itself: it must exist, apply to touch devices,
// cover text inputs, selects and textareas, and must not forbid zooming in the page.
const css = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");
const layout = readFileSync(resolve(process.cwd(), "app/layout.tsx"), "utf8");

describe("text fields on touch devices (iOS focus zoom)", () => {
  const block = css.slice(css.indexOf("@media (pointer: coarse) {\n  input:not("));

  it("sets 16px for inputs, selects and textareas on coarse pointers", () => {
    expect(block).toContain("@media (pointer: coarse)");
    expect(block).toMatch(/\bselect\b/);
    expect(block).toMatch(/\btextarea\b/);
    expect(block).toMatch(/font-size:\s*16px\s*!important/);
  });

  it("leaves checkboxes, radios and buttons alone", () => {
    for (const type of ["checkbox", "radio", "submit", "button"]) expect(block).toContain(`:not([type="${type}"])`);
  });

  it("does not disable zooming for users (no maximum-scale / user-scalable=no)", () => {
    expect(layout).not.toMatch(/maximum-scale|user-scalable/);
  });
});
