import { describe, expect, it } from "vitest";
import { shortenMiddle } from "@/lib/format";

describe("shortenMiddle", () => {
  it("leaves short values alone", () => {
    expect(shortenMiddle("10.0.0.1")).toBe("10.0.0.1");
    expect(shortenMiddle("a".repeat(22))).toBe("a".repeat(22));
  });

  it("keeps the start and the end of a long value and never exceeds the limit", () => {
    const v = "fe80::9618:82ff:fe11:2233:4455";
    const out = shortenMiddle(v, 22);
    expect(out.length).toBeLessThanOrEqual(22);
    expect(out.startsWith("fe80::9618")).toBe(true);
    expect(out.endsWith("2233:4455")).toBe(true);
    expect(out).toContain("…");
  });

  it("keeps two addresses that differ only at the end distinguishable", () => {
    const a = shortenMiddle("fe80::be24:11ff:fe1a:aaaa:0001", 22);
    const b = shortenMiddle("fe80::be24:11ff:fe1a:aaaa:0002", 22);
    expect(a).not.toBe(b);
  });
});
