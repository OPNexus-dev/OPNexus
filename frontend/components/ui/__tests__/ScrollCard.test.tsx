import { act, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScrollCard } from "@/components/ui/ScrollCard";

describe("ScrollCard", () => {
  it("is a card whose content scrolls inside a hinted row, not a bare overflow container", () => {
    const { container } = render(<ScrollCard className="mb-8"><table><tbody><tr><td>x</td></tr></tbody></table></ScrollCard>);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain("mb-8");
    expect(card.className).toContain("rounded-2xl");
    expect(card.className).not.toContain("overflow-x-auto");
    expect(card.querySelector("div.overflow-x-auto")).not.toBeNull();
  });

  it("shows an edge fade and becomes keyboard-focusable when the content is wider than the card", () => {
    const { container } = render(<ScrollCard><table><tbody><tr><td>x</td></tr></tbody></table></ScrollCard>);
    const row = container.querySelector("div.overflow-x-auto")!;
    Object.defineProperty(row, "scrollWidth", { configurable: true, value: 900 });
    Object.defineProperty(row, "clientWidth", { configurable: true, value: 300 });
    act(() => { row.dispatchEvent(new Event("scroll")); });
    expect(container.querySelector('[data-fade="right"]')).not.toBeNull();
    expect(row).toHaveAttribute("tabindex", "0");
  });
});
