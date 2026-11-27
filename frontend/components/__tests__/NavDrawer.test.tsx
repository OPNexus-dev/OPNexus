import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/rules", useRouter: () => ({ push: () => {} }) }));
vi.mock("@/lib/api", () => ({ api: { me: () => Promise.resolve({ authenticated: true }), logout: () => Promise.resolve() } }));

import { NavDrawer } from "@/components/NavDrawer";
import { navGroups } from "@/components/Nav";

// jsdom cannot navigate; the drawer only has to call onClose when a link is chosen.
document.addEventListener("click", (e) => { if ((e.target as HTMLElement).closest("a")) e.preventDefault(); });

const props = { open: true, onClose: () => {}, pathname: "/rules", onLogout: () => {}, id: "menu" };

describe("NavDrawer", () => {
  it("renders nothing while closed", () => {
    render(<NavDrawer {...props} open={false} />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is a modal dialog listing every group and every page that is not hidden", () => {
    render(<NavDrawer {...props} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const links = within(dialog).getAllByRole("link");
    const expected = navGroups.flatMap((g) => g.items).filter((i) => !i.hideInNav).length;
    expect(links).toHaveLength(expected);
    expect(within(dialog).getAllByRole("heading", { level: 2 })).toHaveLength(navGroups.length);
  });

  it("does not list the setup assistant (it stays reachable through the search and the settings)", () => {
    render(<NavDrawer {...props} />);
    expect(screen.queryByRole("link", { name: /setup/i })).toBeNull();
    expect(document.querySelector('a[href="/setup"]')).toBeNull();
  });

  it("marks the current page", () => {
    render(<NavDrawer {...props} />);
    const current = document.querySelector('a[aria-current="page"]');
    expect(current).toHaveAttribute("href", "/rules");
  });

  it("closes with Escape, on a tap next to it and after choosing a page", () => {
    const onClose = vi.fn();
    render(<NavDrawer {...props} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getAllByRole("button", { name: "nav.closeMenu" })[0]);
    expect(onClose).toHaveBeenCalledTimes(2);
    fireEvent.click(document.querySelector('a[href="/hosts"]')!);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("keeps the focus inside while open (Tab from the last element goes to the first)", () => {
    render(<NavDrawer {...props} />);
    const dialog = screen.getByRole("dialog");
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
    focusables[focusables.length - 1].focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(focusables[0]);
    focusables[0].focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(focusables[focusables.length - 1]);
  });

  it("gives every entry the touch height (44 px)", () => {
    render(<NavDrawer {...props} />);
    for (const link of screen.getByRole("dialog").querySelectorAll("nav a")) expect(link.className).toContain("min-h-11");
  });
});
