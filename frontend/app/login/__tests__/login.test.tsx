import { fireEvent, render, waitFor } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const login = vi.fn().mockResolvedValue({ last_login_at: null });
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/login" }));
vi.mock("@/components/NetworkBackground", () => ({ NetworkBackground: () => null }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, oidcConfig: vi.fn().mockResolvedValue({ enabled: false, name: null }), login: (...args: unknown[]) => login(...args) } };
});

import LoginPage from "../page";

describe("login form", () => {
  it("declares method=post as a backstop against a native submit", () => {
    const { container } = render(<LoginPage />);
    const form = container.querySelector("form")!;
    expect(form.getAttribute("method")).toBe("post");
  });

  it("keeps the Sign in button disabled until the page is hydrated, so nothing can submit natively before", () => {
    // render() runs effects, so \"ready\" is already set here; the server-rendered markup is what a browser sees before hydration
    const html = renderToString(<LoginPage />);
    expect(html).toMatch(/<button[^>]*type=\"submit\"[^>]*disabled/);
  });

  it("signs in through the API (the submit handler runs, not a native submit)", async () => {
    const { container } = render(<LoginPage />);
    fireEvent.change(container.querySelector('input[autocomplete="username"]')!, { target: { value: "admin" } });
    fireEvent.change(container.querySelector('input[type="password"]')!, { target: { value: "secret" } });
    const button = container.querySelector<HTMLButtonElement>('button[type="submit"]')!;   // no language provider here, so no text to look for
    await waitFor(() => expect(button).toBeEnabled());   // enabled once the page is hydrated
    fireEvent.click(button);
    await waitFor(() => expect(login).toHaveBeenCalledWith("admin", "secret", undefined));
  });
});
