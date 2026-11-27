import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

const login = vi.fn().mockResolvedValue({ last_login_at: null });
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/login" }));
vi.mock("@/components/NetworkBackground", () => ({ NetworkBackground: () => null }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, oidcConfig: vi.fn().mockResolvedValue({ enabled: false, name: null }), login: (...args: unknown[]) => login(...args) } };
});

import { ApiError } from "@/lib/api";
import LoginPage from "../page";

describe("login form", () => {
  const hydrated: { root: ReturnType<typeof hydrateRoot>; container: HTMLElement }[] = [];
  afterEach(() => { hydrated.splice(0).forEach(({ root, container }) => { act(() => root.unmount()); container.remove(); }); });

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

  // What a browser does: the server markup is on screen, the person (or a password manager) types into it, and only then does the JavaScript start.
  async function hydrateAfterTyping(typed: { username?: string; password?: string }) {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LoginPage />);
    document.body.appendChild(container);
    const user = container.querySelector<HTMLInputElement>('input[autocomplete="username"]')!;
    const pass = container.querySelector<HTMLInputElement>('input[type="password"]')!;
    if (typed.username !== undefined) user.value = typed.username;
    if (typed.password !== undefined) pass.value = typed.password;
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    let root!: ReturnType<typeof hydrateRoot>;
    await act(async () => { root = hydrateRoot(container, <LoginPage />); });
    hydrated.push({ root, container });
    expect(errors).not.toHaveBeenCalled();   // no hydration mismatch warning
    errors.mockRestore();
    return { container, user, pass, button: container.querySelector<HTMLButtonElement>('button[type="submit"]')! };
  }

  it("keeps what was typed before the JavaScript started", async () => {
    const { user, pass, button } = await hydrateAfterTyping({ username: "admin", password: "secret" });
    await waitFor(() => expect(button).toBeEnabled());
    expect(user.value).toBe("admin");
    expect(pass.value).toBe("secret");
  });

  it("signs in with the values typed before the JavaScript started, without touching the fields again", async () => {
    login.mockClear();
    const { button } = await hydrateAfterTyping({ username: "admin", password: "secret" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(login).toHaveBeenCalledWith("admin", "secret", undefined));
  });

  it("takes a value filled in by a password manager without any change event", async () => {
    login.mockClear();
    const { container, user, pass, button } = await hydrateAfterTyping({});
    await waitFor(() => expect(button).toBeEnabled());
    user.value = "ops";   // set directly: no input/change event reaches React
    pass.value = "filled-in";
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() => expect(login).toHaveBeenCalledWith("ops", "filled-in", undefined));
  });

  it("still follows later typing", async () => {
    login.mockClear();
    const { user, pass, button } = await hydrateAfterTyping({ username: "early" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.change(user, { target: { value: "late" } });
    fireEvent.change(pass, { target: { value: "pw" } });
    fireEvent.click(button);
    await waitFor(() => expect(login).toHaveBeenCalledWith("late", "pw", undefined));
  });

  describe("second step (authenticator code)", () => {
    async function toCodeStep() {
      login.mockClear();
      login.mockRejectedValueOnce(new ApiError(401, "code required", "TOTP_REQUIRED"));
      const { container, user, pass, button } = await hydrateAfterTyping({ username: "admin", password: "secret" });
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      const codeField = await waitFor(() => {
        const el = container.querySelector<HTMLInputElement>('input[autocomplete="one-time-code"]');
        expect(el).not.toBeNull();
        return el!;
      });
      return { container, codeField, user, pass };
    }

    it("sends the first-step values together with the code", async () => {
      const { container, codeField } = await toCodeStep();
      expect(container.querySelector('input[type="password"]')).toBeNull();
      codeField.value = " 123456 ";   // set directly, like an autofill that raises no change event
      fireEvent.submit(container.querySelector("form")!);
      await waitFor(() => expect(login).toHaveBeenLastCalledWith("admin", "secret", "123456"));
    });

    it("keeps the user name but not the password when going back", async () => {
      const { container } = await toCodeStep();
      fireEvent.click(container.querySelector<HTMLButtonElement>('button[type="button"]')!);
      await waitFor(() => expect(container.querySelector('input[type="password"]')).not.toBeNull());
      expect(container.querySelector<HTMLInputElement>('input[autocomplete="username"]')!.value).toBe("admin");
      expect(container.querySelector<HTMLInputElement>('input[type="password"]')!.value).toBe("");
    });
  });
});
