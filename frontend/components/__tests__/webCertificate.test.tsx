import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const INFO = { subject: "CN=lab", issuer: "CN=Lab CA", not_after: "2030-01-01T00:00:00Z", days_left: 900, names: ["IP:10.0.0.5", "DNS:opnexus.lan"], fingerprint: "AA:BB:CC", uncovered: [] as string[] };
const internal = { mode: "auto", source: "all" as const, custom_not_served: false, ...INFO, subject: "CN=OPNexus", issuer: "CN=Caddy Local Authority" };
const own = { ...internal, mode: "custom", source: "custom" as const, subject: "CN=lab", issuer: "CN=Lab CA" };

let webCert: ReturnType<typeof vi.fn>;
let webCertCheck: ReturnType<typeof vi.fn>;
let webCertInstall: ReturnType<typeof vi.fn>;
let webCertRemove: ReturnType<typeof vi.fn>;
let confirmAnswer = true;
vi.mock("@/lib/api", () => ({
  api: {
    webCert: () => webCert(),
    webCertCheck: (c: string, k: string) => webCertCheck(c, k),
    webCertInstall: (c: string, k: string) => webCertInstall(c, k),
    webCertRemove: () => webCertRemove(),
  },
}));
vi.mock("@/components/ui/Confirm", () => ({ useConfirm: () => () => Promise.resolve(confirmAnswer) }));

import { WebCertificateSection } from "@/components/WebCertificateSection";

const state = (over: Record<string, unknown> = {}) => ({ available: true, status: internal, error: null, job: null, log: [], ha_role: null, ...over });
const job = (over: Record<string, unknown> = {}) => ({ id: "j1", verb: "install", state: "running", started_at: "t", finished_at: null, exit_code: null, ...over });

async function shown() {
  render(<WebCertificateSection />);
  await screen.findByText("webCert.title");
}

describe("Web certificate section", () => {
  beforeEach(() => {
    confirmAnswer = true;
    webCert = vi.fn(() => Promise.resolve(state()));
    webCertCheck = vi.fn(() => Promise.resolve({ certificate: INFO }));
    webCertInstall = vi.fn(() => Promise.resolve({ job: job() }));
    webCertRemove = vi.fn(() => Promise.resolve({ job: job({ id: "j2", verb: "remove" }) }));
  });
  afterEach(() => vi.useRealTimers());

  it("shows nothing when the status is refused (not an admin)", async () => {
    webCert = vi.fn(() => Promise.reject(new Error("forbidden")));
    const { container } = render(<WebCertificateSection />);
    await waitFor(() => expect(webCert).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("explains how to proceed without the updater and offers no buttons", async () => {
    webCert = vi.fn(() => Promise.resolve({ available: false, reason: "not_enabled" }));
    await shown();
    expect(screen.getByText("webCert.unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /webCert\.(install|remove)/ })).toBeNull();
  });

  it("shows source, addresses and fingerprint of the served certificate", async () => {
    await shown();
    expect(await screen.findByText("webCert.sourceAll")).toBeInTheDocument();
    expect(screen.getByText("DNS:opnexus.lan")).toBeInTheDocument();
    expect(screen.getByText("AA:BB:CC")).toBeInTheDocument();
    expect(screen.getByText("webCert.badgeInternal")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "webCert.remove" })).toBeNull();   // nothing to go back from
  });

  it("warns about addresses the certificate does not cover and about an own certificate that is not served", async () => {
    webCert = vi.fn(() => Promise.resolve(state({ status: { ...own, uncovered: ["other.lan"], custom_not_served: true } })));
    await shown();
    expect(await screen.findByText("webCert.uncovered")).toBeInTheDocument();
    expect(screen.getByText("webCert.notServed")).toBeInTheDocument();
    expect(screen.getByText("webCert.noRenew")).toBeInTheDocument();
  });

  it("flags a certificate that runs out soon or already did", async () => {
    webCert = vi.fn(() => Promise.resolve(state({ status: { ...own, days_left: 5 } })));
    await shown();
    expect(await screen.findByText("webCert.daysLeft")).toBeInTheDocument();            // the badge
    expect(screen.getByText(/\(webCert\.daysLeft\)/)).toBeInTheDocument();               // and the date line
    expect(screen.queryByText("webCert.expired")).toBeNull();
  });

  it("marks an expired certificate", async () => {
    webCert = vi.fn(() => Promise.resolve(state({ status: { ...own, days_left: -3 } })));
    await shown();
    expect(await screen.findByText("webCert.expired")).toBeInTheDocument();
    expect(screen.getByText(/\(webCert\.expired\)/)).toBeInTheDocument();
  });

  it("shows nothing alarming for a certificate with plenty of time left", async () => {
    await shown();
    await screen.findByText("webCert.sourceAll");
    expect(screen.queryByText("webCert.expired")).toBeNull();
    expect(screen.getByText(/\(webCert\.daysLeft\)/)).toBeInTheDocument();
    expect(screen.queryByText("webCert.daysLeft")).toBeNull();                           // no warning badge
  });

  it("states that the certificate reaches the standby on a primary, and leaves changes to the primary on a standby", async () => {
    webCert = vi.fn(() => Promise.resolve(state({ ha_role: "primary" })));
    await shown();
    expect(await screen.findByText("webCert.haPrimary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "webCert.install" })).toBeEnabled();
  });

  it("disables changes on the standby", async () => {
    webCert = vi.fn(() => Promise.resolve(state({ ha_role: "standby", status: own })));
    await shown();
    expect(await screen.findByText("webCert.haStandby")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "webCert.install" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "webCert.remove" })).toBeDisabled();
  });

  describe("install dialog", () => {
    async function openDialog() {
      await shown();
      fireEvent.click(await screen.findByRole("button", { name: "webCert.install" }));
      return { cert: screen.getByLabelText("webCert.certificate"), key: screen.getByLabelText("webCert.key") };
    }
    const checkBtn = () => screen.getByRole("button", { name: "webCert.check" });
    const installBtn = () => screen.getByRole("button", { name: "webCert.installNow" });

    it("keeps both buttons grey until certificate and key are filled, and Install grey until they were checked", async () => {
      const { cert, key } = await openDialog();
      expect(checkBtn()).toBeDisabled(); expect(installBtn()).toBeDisabled();
      fireEvent.change(cert, { target: { value: "CERT" } });
      expect(checkBtn()).toBeDisabled();                  // key missing
      fireEvent.change(key, { target: { value: "KEY" } });
      expect(checkBtn()).toBeEnabled(); expect(installBtn()).toBeDisabled();
    });

    it("checks exactly what was typed and then allows installing", async () => {
      const { cert, key } = await openDialog();
      fireEvent.change(cert, { target: { value: "CERT" } }); fireEvent.change(key, { target: { value: "KEY" } });
      await act(async () => { fireEvent.click(checkBtn()); });
      await waitFor(() => expect(webCertCheck).toHaveBeenCalledWith("CERT", "KEY"));
      expect(await screen.findByText("webCert.checkOk")).toBeInTheDocument();
      expect(installBtn()).toBeEnabled(); expect(checkBtn()).toBeDisabled();     // nothing left to check
    });

    it("withdraws the approval as soon as the text changes after the check", async () => {
      const { cert, key } = await openDialog();
      fireEvent.change(cert, { target: { value: "CERT" } }); fireEvent.change(key, { target: { value: "KEY" } });
      await act(async () => { fireEvent.click(checkBtn()); });
      await screen.findByText("webCert.checkOk");
      fireEvent.change(key, { target: { value: "OTHER KEY" } });
      expect(screen.queryByText("webCert.checkOk")).toBeNull();
      expect(installBtn()).toBeDisabled(); expect(checkBtn()).toBeEnabled();
    });

    it("shows the reason of a refused check and keeps Install grey", async () => {
      webCertCheck = vi.fn(() => Promise.reject(Object.assign(new Error("refused"), { status: 400 })));
      const { cert, key } = await openDialog();
      fireEvent.change(cert, { target: { value: "CERT" } }); fireEvent.change(key, { target: { value: "KEY" } });
      await act(async () => { fireEvent.click(checkBtn()); });
      expect(await screen.findByRole("alert")).toBeInTheDocument();
      expect(installBtn()).toBeDisabled();
    });

    it("warns about addresses the new certificate does not cover", async () => {
      webCertCheck = vi.fn(() => Promise.resolve({ certificate: { ...INFO, uncovered: ["other.lan"] } }));
      const { cert, key } = await openDialog();
      fireEvent.change(cert, { target: { value: "CERT" } }); fireEvent.change(key, { target: { value: "KEY" } });
      await act(async () => { fireEvent.click(checkBtn()); });
      expect(await screen.findByText("webCert.checkNotCovered")).toBeInTheDocument();
    });

    it("installs, closes the dialog and shows the running job (buttons grey while it runs)", async () => {
      const { cert, key } = await openDialog();
      fireEvent.change(cert, { target: { value: "CERT" } }); fireEvent.change(key, { target: { value: "KEY" } });
      await act(async () => { fireEvent.click(checkBtn()); });
      await screen.findByText("webCert.checkOk");
      await act(async () => { fireEvent.click(installBtn()); });
      await waitFor(() => expect(webCertInstall).toHaveBeenCalledWith("CERT", "KEY"));
      await waitFor(() => expect(screen.queryByLabelText("webCert.key")).toBeNull());     // dialog (and the key in it) is gone
      expect(await screen.findByText("webCert.running")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "webCert.install" })).toBeDisabled();
    });
  });

  describe("job", () => {
    it("polls while a job runs, survives failing requests and reports success", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      let n = 0;
      webCert = vi.fn(() => {
        n += 1;
        if (n === 1) return Promise.resolve(state({ job: job() }));                         // page opened during a running job
        if (n === 2) return Promise.reject(new Error("connection reset"));                   // proxy recreated
        return Promise.resolve(state({ status: own, job: job({ state: "succeeded", exit_code: 0 }) }));
      });
      await shown();
      expect(await screen.findByText("webCert.running")).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(screen.getByText("webCert.running")).toBeInTheDocument();                       // the failed poll changed nothing
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      await waitFor(() => expect(screen.getByText("webCert.doneInstall")).toBeInTheDocument());
      expect(screen.queryByText("webCert.running")).toBeNull();
      expect(screen.getByRole("button", { name: "webCert.remove" })).toBeEnabled();
    });

    it("reports a failed job with its log", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      let n = 0;
      webCert = vi.fn(() => Promise.resolve(++n === 1 ? state({ job: job() }) : state({ job: job({ state: "failed", exit_code: 1 }), log: ["Der Proxy liefert das neue Zertifikat nicht aus"] })));
      await shown();
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(await screen.findByText("webCert.failed")).toBeInTheDocument();
      expect(screen.getByText(/Der Proxy liefert das neue Zertifikat nicht aus/)).toBeInTheDocument();
    });

    it("gives up after a long silence and says so", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      let n = 0;
      webCert = vi.fn(() => (++n === 1 ? Promise.resolve(state({ job: job() })) : Promise.reject(new Error("down"))));
      await shown();
      await act(async () => { await vi.advanceTimersByTimeAsync(160_000); });
      expect(await screen.findByText("webCert.lostConnection")).toBeInTheDocument();
    });
  });

  describe("back to the internal certificate", () => {
    it("asks first and starts the removal", async () => {
      webCert = vi.fn(() => Promise.resolve(state({ status: own })));
      await shown();
      await act(async () => { fireEvent.click(await screen.findByRole("button", { name: "webCert.remove" })); });
      await waitFor(() => expect(webCertRemove).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("webCert.running")).toBeInTheDocument();
    });

    it("does nothing when the question is declined", async () => {
      confirmAnswer = false;
      webCert = vi.fn(() => Promise.resolve(state({ status: own })));
      await shown();
      await act(async () => { fireEvent.click(await screen.findByRole("button", { name: "webCert.remove" })); });
      await new Promise((r) => setTimeout(r, 20));
      expect(webCertRemove).not.toHaveBeenCalled();
    });
  });
});
