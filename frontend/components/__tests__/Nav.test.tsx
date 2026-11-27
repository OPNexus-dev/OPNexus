import "@/test/viewport";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { setViewport } from "@/test/viewport";

let pathname = "/rules";
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => pathname, useRouter: () => ({ push: () => {} }) }));
vi.mock("@/lib/api", () => ({ api: { me: () => Promise.resolve({ authenticated: true, username: "julian", role: "admin" }), logout: () => Promise.resolve() } }));

import { Nav, navGroups } from "@/components/Nav";

describe("Nav", () => {
  it("desktop: group links carry their label, no menu button", async () => {
    setViewport(1440);
    render(<Nav />);
    for (const group of navGroups) expect(screen.getByRole("link", { name: `nav.groups.${group.key}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "nav.openMenu" })).toBeNull();
    await act(async () => {});
  });

  it("desktop: the sub navigation lists the pages of the active group with labels and marks the current one", async () => {
    setViewport(1440);
    render(<Nav />);
    const sub = screen.getByRole("navigation", { name: "nav.subNavigation" });
    expect(sub.querySelector('a[aria-current="page"]')).toHaveAttribute("href", "/rules");
    expect(sub.textContent).toContain("nav.items.analysis");
    await act(async () => {});
  });

  it("tablet (820 px): menu button instead of the group row", async () => {
    setViewport(820);
    render(<Nav />);
    expect(screen.getByRole("button", { name: "nav.openMenu" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "nav.groups.policy" })).toBeNull();
    await act(async () => {});
  });

  it("phone: the menu opens as a dialog and the button reports it", async () => {
    setViewport(390);
    render(<Nav />);
    const button = screen.getByRole("button", { name: "nav.openMenu" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "nav.openMenu" })).toHaveAttribute("aria-expanded", "true");
    await act(async () => {});
  });

  it("phone: the sub navigation names the group it belongs to, outside the scrolling row", async () => {
    setViewport(390);
    const { container } = render(<Nav />);
    const label = screen.getByText("nav.groups.policy");
    expect(label.closest("div.overflow-x-auto")).toBeNull();
    expect(container.querySelector("div.overflow-x-auto")).not.toBeNull();
    await act(async () => {});
  });

  it("the setup page is not part of the System sub navigation", async () => {
    setViewport(1440);
    pathname = "/settings";
    render(<Nav />);
    const sub = screen.getByRole("navigation", { name: "nav.subNavigation" });
    expect(sub.querySelector('a[href="/setup"]')).toBeNull();
    expect(sub.querySelector('a[href="/settings"]')).not.toBeNull();
    pathname = "/rules";
    await act(async () => {});
  });
});
