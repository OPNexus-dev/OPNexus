import { act, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScrollRow } from "@/components/ui/ScrollRow";

function setGeometry(el: HTMLElement, { scrollWidth, clientWidth, scrollLeft = 0 }: { scrollWidth: number; clientWidth: number; scrollLeft?: number }) {
  Object.defineProperty(el, "scrollWidth", { configurable: true, value: scrollWidth });
  Object.defineProperty(el, "clientWidth", { configurable: true, value: clientWidth });
  Object.defineProperty(el, "scrollLeft", { configurable: true, writable: true, value: scrollLeft });
}

describe("ScrollRow", () => {
  it("shows no fade when everything fits", () => {
    const { container } = render(<ScrollRow><a href="/a">A</a></ScrollRow>);
    expect(container.querySelector("[data-fade]")).toBeNull();
  });

  it("shows a fade on the right when content is cut off, and on the left after scrolling", () => {
    const { container } = render(<ScrollRow><a href="/a">A</a></ScrollRow>);
    const row = container.querySelector<HTMLElement>("div.overflow-x-auto")!;
    setGeometry(row, { scrollWidth: 900, clientWidth: 300 });
    act(() => { row.dispatchEvent(new Event("scroll")); });
    expect(container.querySelector('[data-fade="right"]')).not.toBeNull();
    expect(container.querySelector('[data-fade="left"]')).toBeNull();
    setGeometry(row, { scrollWidth: 900, clientWidth: 300, scrollLeft: 200 });
    act(() => { row.dispatchEvent(new Event("scroll")); });
    expect(container.querySelector('[data-fade="left"]')).not.toBeNull();
    expect(container.querySelector('[data-fade="right"]')).not.toBeNull();
  });

  it("hides the fade at the end of the row", () => {
    const { container } = render(<ScrollRow><a href="/a">A</a></ScrollRow>);
    const row = container.querySelector<HTMLElement>("div.overflow-x-auto")!;
    setGeometry(row, { scrollWidth: 900, clientWidth: 300, scrollLeft: 600 });
    act(() => { row.dispatchEvent(new Event("scroll")); });
    expect(container.querySelector('[data-fade="right"]')).toBeNull();
  });

  it("the fade is decoration only (hidden from screen readers, no pointer events)", () => {
    const { container } = render(<ScrollRow><a href="/a">A</a></ScrollRow>);
    const row = container.querySelector<HTMLElement>("div.overflow-x-auto")!;
    setGeometry(row, { scrollWidth: 900, clientWidth: 300 });
    act(() => { row.dispatchEvent(new Event("scroll")); });
    const fade = container.querySelector('[data-fade="right"]')!;
    expect(fade).toHaveAttribute("aria-hidden", "true");
    expect(fade.className).toContain("pointer-events-none");
  });
});
