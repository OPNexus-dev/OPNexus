import "@/test/viewport";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { setViewport } from "@/test/viewport";
import { breakpointFor, useBreakpoint } from "@/lib/useBreakpoint";

describe("breakpointFor", () => {
  it("uses the Tailwind limits 768 and 1024", () => {
    expect(breakpointFor(390)).toBe("phone");
    expect(breakpointFor(767)).toBe("phone");
    expect(breakpointFor(768)).toBe("tablet");
    expect(breakpointFor(1023)).toBe("tablet");
    expect(breakpointFor(1024)).toBe("desktop");
    expect(breakpointFor(1920)).toBe("desktop");
  });
});

describe("useBreakpoint", () => {
  it("follows the viewport width", () => {
    setViewport(390);
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("phone");
    act(() => setViewport(820));
    expect(result.current).toBe("tablet");
    act(() => setViewport(1440));
    expect(result.current).toBe("desktop");
  });
});
