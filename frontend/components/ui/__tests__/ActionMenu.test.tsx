import { fireEvent, render, screen, within } from "@testing-library/react";
import { Copy, History, Pencil, Trash2 } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import { ActionMenu, orderActions } from "@/components/ui/ActionMenu";
import { RowActions } from "@/components/ui/RowActions";

const actions = (over: Partial<Record<string, () => void>> = {}) => [
  { key: "delete", label: "Delete", icon: Trash2, tone: "danger" as const, onSelect: over.delete },
  { key: "history", label: "History", icon: History, onSelect: over.history },
  { key: "copy", label: "Clone", icon: Copy, onSelect: over.copy },
];

describe("orderActions", () => {
  it("puts destructive entries last, keeping the order of the others", () => {
    expect(orderActions(actions()).map((a) => a.key)).toEqual(["history", "copy", "delete"]);
  });
});

describe("ActionMenu", () => {
  it("is a menu button that reports its state", () => {
    render(<ActionMenu actions={actions()} label="More" />);
    const button = screen.getByRole("button", { name: "More" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("lists the entries with the destructive one last, behind a separator", () => {
    render(<ActionMenu actions={actions()} label="More" />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    const items = screen.getAllByRole("menuitem").map((i) => i.textContent);
    expect(items).toEqual(["History", "Clone", "Delete"]);
    expect(screen.getByRole("separator")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Delete" }).className).toContain("text-red-600");
  });

  it("focuses the first entry, moves with the arrow keys and wraps around", () => {
    render(<ActionMenu actions={actions()} label="More" />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    const [first, second, third] = screen.getAllByRole("menuitem");
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(document.activeElement).toBe(second);
    fireEvent.keyDown(document, { key: "End" });
    expect(document.activeElement).toBe(third);
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(document.activeElement).toBe(third);
    fireEvent.keyDown(document, { key: "Home" });
    expect(document.activeElement).toBe(first);
  });

  it("closes with Escape and gives the focus back to the button", () => {
    render(<ActionMenu actions={actions()} label="More" />);
    const button = screen.getByRole("button", { name: "More" });
    fireEvent.click(button);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("closes on a click outside", () => {
    render(<div><ActionMenu actions={actions()} label="More" /><p>elsewhere</p></div>);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    fireEvent.pointerDown(screen.getByText("elsewhere"));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("runs the action after closing the menu", () => {
    const onDelete = vi.fn();
    render(<ActionMenu actions={actions({ delete: onDelete })} label="More" />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("does not run a disabled action and skips it in keyboard navigation", () => {
    const onCopy = vi.fn();
    render(<ActionMenu actions={[{ key: "a", label: "A", onSelect: () => {} }, { key: "b", label: "B", disabled: true, onSelect: onCopy }, { key: "c", label: "C", onSelect: () => {} }]} label="More" />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    expect(screen.getByRole("menuitem", { name: "B" })).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByRole("menuitem", { name: "B" }));
    expect(onCopy).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "C" }));
  });

  it("removes its window listeners when it closes: the number left over does not grow with each opening", () => {
    const live = new Set<EventListenerOrEventListenerObject>();
    const add = vi.spyOn(window, "addEventListener").mockImplementation(function (this: Window, type: string, fn: EventListenerOrEventListenerObject) { if (type === "scroll" || type === "resize") live.add(fn); });
    const remove = vi.spyOn(window, "removeEventListener").mockImplementation(function (this: Window, type: string, fn: EventListenerOrEventListenerObject) { if (type === "scroll" || type === "resize") live.delete(fn); });
    render(<ActionMenu actions={actions()} label="More" />);
    const button = screen.getByRole("button", { name: "More" });
    const cycle = () => { fireEvent.click(button); fireEvent.keyDown(document, { key: "Escape" }); };
    cycle();
    const afterOne = live.size;
    cycle(); cycle(); cycle();
    expect(live.size).toBe(afterOne);
    add.mockRestore(); remove.mockRestore();
  });

  it("owns its items directly and keeps them out of the Tab order (roving focus)", () => {
    render(<ActionMenu actions={[...actions(), { key: "open", label: "Open", href: "/x" }]} label="More" />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    const menu = screen.getByRole("menu");
    for (const child of Array.from(menu.children)) expect(["menuitem", "separator"]).toContain(child.getAttribute("role"));
    for (const item of screen.getAllByRole("menuitem")) expect(item).toHaveAttribute("tabindex", "-1");
  });

  it("renders nothing without actions", () => {
    const { container } = render(<ActionMenu actions={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a link entry for actions with href", () => {
    render(<ActionMenu actions={[{ key: "open", label: "Open", href: "/hosts/1" }]} label="More" />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    expect(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Open" })).toHaveAttribute("href", "/hosts/1");
  });
});

describe("RowActions", () => {
  it("shows two icon buttons and moves the rest, destructive ones always, into the menu", () => {
    render(<RowActions actions={[
      { key: "edit", label: "Edit", icon: Pencil, onSelect: () => {} },
      { key: "deploy", label: "Deploy", icon: Copy, onSelect: () => {} },
      { key: "history", label: "History", icon: History, onSelect: () => {} },
      { key: "delete", label: "Delete", icon: Trash2, tone: "danger", onSelect: () => {} },
    ]} />);
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "History" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "common.moreActions" }));
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["History", "Delete"]);
  });

  it("has no menu button when everything fits and nothing is destructive", () => {
    render(<RowActions actions={[{ key: "edit", label: "Edit", icon: Pencil, onSelect: () => {} }]} />);
    expect(screen.queryByRole("button", { name: "common.moreActions" })).toBeNull();
  });

  it("a destructive action is never shown next to the main actions, even if it is the only one", () => {
    render(<RowActions actions={[{ key: "delete", label: "Delete", icon: Trash2, tone: "danger", onSelect: () => {} }]} />);
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.getByRole("button", { name: "common.moreActions" })).toBeInTheDocument();
  });

  it("a disabled direct action does not fire", () => {
    const onEdit = vi.fn();
    render(<RowActions actions={[{ key: "edit", label: "Edit", icon: Pencil, disabled: true, onSelect: onEdit }]} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEdit).not.toHaveBeenCalled();
  });
});
