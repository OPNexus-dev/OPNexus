import { act, fireEvent, render, screen } from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let path = "/login";
vi.mock("next/navigation", () => ({ usePathname: () => path }));
vi.mock("@/lib/api", () => ({ api: { me: vi.fn().mockResolvedValue({ authenticated: false }), settings: vi.fn() } }));

import { ThemeProvider, useTheme } from "../ThemeProvider";

function Probe() {
  const { theme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={() => setTheme("light")}>light</button>
    </div>
  );
}

// jsdom has no matchMedia: the operating system is "dark" or "light"
function setSystem(dark: boolean) {
  window.matchMedia = ((query: string) => ({ matches: dark && query.includes("dark"), media: query, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, onchange: null, dispatchEvent: () => false })) as typeof window.matchMedia;
}

// Every value data-theme has had since the observer was attached (the inline script in the layout has set the first one before React runs)
function watchDataTheme() {
  const root = document.documentElement;
  const records: string[] = [];
  const observer = new MutationObserver((list) => list.forEach((r) => records.push(r.oldValue ?? "")));
  observer.observe(root, { attributes: true, attributeFilter: ["data-theme"], attributeOldValue: true });
  return {
    seen: () => { observer.takeRecords().forEach((r) => records.push(r.oldValue ?? "")); return [...records.slice(1), root.dataset.theme ?? ""]; },   // old values shift by one: drop the initial one, add the current
    stop: () => observer.disconnect(),
  };
}

describe("ThemeProvider", () => {
  beforeEach(() => { path = "/login"; localStorage.clear(); delete document.documentElement.dataset.theme; });
  afterEach(() => { vi.restoreAllMocks(); });

  it("never shows light for a stored dark theme on a light system (no flash while loading)", async () => {
    setSystem(false);
    localStorage.setItem("opnexus-theme", "dark");
    document.documentElement.dataset.theme = "dark";   // what the inline script in app/layout.tsx has set
    const watch = watchDataTheme();
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    expect(screen.getByTestId("theme").textContent).toBe("dark");
    expect(watch.seen()).not.toContain("light");
    expect(document.documentElement.dataset.theme).toBe("dark");
    watch.stop();
  });

  it("never shows dark for a stored light theme on a dark system", async () => {
    setSystem(true);
    localStorage.setItem("opnexus-theme", "light");
    document.documentElement.dataset.theme = "light";
    const watch = watchDataTheme();
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    expect(watch.seen()).not.toContain("dark");
    expect(document.documentElement.dataset.theme).toBe("light");
    watch.stop();
  });

  it.each([[null], ["system"], ["blue"]])("follows the system when the stored value is %s", async (stored) => {
    setSystem(true);
    if (stored) localStorage.setItem("opnexus-theme", stored);
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("does not set light while a stored 'system' theme loads on a dark system", async () => {
    setSystem(true);
    localStorage.setItem("opnexus-theme", "system");
    document.documentElement.dataset.theme = "dark";
    const watch = watchDataTheme();
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    expect(watch.seen()).not.toContain("light");
    watch.stop();
  });

  it("copes with unreadable storage (falls back to the system)", async () => {
    setSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("applies a change made in the settings and stores it", async () => {
    setSystem(false);
    localStorage.setItem("opnexus-theme", "dark");
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    fireEvent.click(screen.getByText("light"));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("opnexus-theme")).toBe("light");
  });

  it("follows a change made in another tab (storage event)", async () => {
    setSystem(false);
    localStorage.setItem("opnexus-theme", "light");
    await act(async () => { render(<ThemeProvider><Probe /></ThemeProvider>); });
    await act(async () => { window.dispatchEvent(new StorageEvent("storage", { key: "opnexus-theme", newValue: "dark" })); });
    expect(screen.getByTestId("theme").textContent).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("hydrates the server markup without a mismatch warning, although a theme is stored", async () => {
    setSystem(false);
    localStorage.setItem("opnexus-theme", "dark");
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const container = document.createElement("div");
    container.innerHTML = renderToString(<ThemeProvider><Probe /></ThemeProvider>);   // the server knows nothing about localStorage
    document.body.appendChild(container);
    let root!: ReturnType<typeof hydrateRoot>;
    await act(async () => { root = hydrateRoot(container, <ThemeProvider><Probe /></ThemeProvider>); });
    expect(errors).not.toHaveBeenCalled();
    expect(container.querySelector("[data-testid=theme]")!.textContent).toBe("dark");   // picked up right after hydrating
    act(() => root.unmount());
    container.remove();
  });
});
