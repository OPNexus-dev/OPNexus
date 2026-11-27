import { describe, expect, it } from "vitest";
import { auditTone, sinceFor } from "@/lib/audit";

describe("auditTone", () => {
  it("marks failures red, in-progress states orange, success green", () => {
    for (const s of ["error", "revert_failed", "denied", "rejected"]) expect(auditTone(s)).toBe("danger");
    for (const s of ["pending", "starting", "submitted", "interrupted", "unknown"]) expect(auditTone(s)).toBe("warning");
    expect(auditTone("success")).toBe("success");
    for (const s of ["reverted", "removed", "acknowledged"]) expect(auditTone(s)).toBe("info");
    for (const s of ["requested", "accepted", "whatever"]) expect(auditTone(s)).toBe("neutral");
  });
});

describe("sinceFor", () => {
  const now = new Date("2026-11-26T12:00:00Z");
  it("returns the start of the range", () => {
    expect(sinceFor("24h", now)).toBe("2026-11-25T12:00:00.000Z");
    expect(sinceFor("7d", now)).toBe("2026-11-19T12:00:00.000Z");
    expect(sinceFor("30d", now)).toBe("2026-10-27T12:00:00.000Z");
  });
  it("returns nothing for no range or an unknown one", () => {
    expect(sinceFor("", now)).toBe("");
    expect(sinceFor("1y", now)).toBe("");
  });
});
