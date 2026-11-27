import { describe, expect, it } from "vitest";
import { isUnmonitored } from "@/components/GatewayStatus";

describe("isUnmonitored", () => {
  it("is true only for a gateway without loss, delay and a known state", () => {
    expect(isUnmonitored({ state: "unknown", loss: null, delay: null })).toBe(true);
    expect(isUnmonitored({ state: "online", loss: 0, delay: 5 })).toBe(false);
    expect(isUnmonitored({ state: "unknown", loss: 0, delay: null })).toBe(false);
    expect(isUnmonitored({ state: "offline", loss: null, delay: null })).toBe(false);
  });
});
