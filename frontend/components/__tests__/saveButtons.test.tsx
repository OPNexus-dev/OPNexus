import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const stored = {
  syslog_enabled: true, syslog_host: "syslog.lan", syslog_port: 514, syslog_protocol: "udp" as const,
  webhook_enabled: false, webhook_url: null, webhook_signing_secret_set: false,
  last_success_at: null, last_error: null, last_error_at: null,
};
let update: ReturnType<typeof vi.fn>;
vi.mock("@/lib/api", () => ({
  api: {
    getAuditExport: () => Promise.resolve({ ...stored }),
    updateAuditExport: (body: Record<string, unknown>) => update(body),
  },
}));

import { AuditExportSection } from "@/components/AuditExportSection";

// The settings sections keep Save grey while there is nothing to save (docs: Nutzerwunsch 2026-11-26).
describe("Save button of a settings section", () => {
  beforeEach(() => {
    update = vi.fn((body: Record<string, unknown>) => Promise.resolve({ ...stored, ...body }));
  });

  async function loaded() {
    render(<AuditExportSection />);
    await waitFor(() => expect((screen.getByDisplayValue("syslog.lan") as HTMLInputElement).value).toBe("syslog.lan"));
    return screen.getByRole("button", { name: "common.save" });
  }

  it("is grey when the form equals the stored settings", async () => {
    expect(await loaded()).toBeDisabled();
  });

  it("is active after a change and grey again when the change is undone", async () => {
    const save = await loaded();
    const host = screen.getByDisplayValue("syslog.lan");
    fireEvent.change(host, { target: { value: "other.lan" } });
    expect(save).toBeEnabled();
    fireEvent.change(host, { target: { value: "syslog.lan" } });
    expect(save).toBeDisabled();
  });

  it("is grey again after a successful save", async () => {
    const save = await loaded();
    fireEvent.change(screen.getByDisplayValue("syslog.lan"), { target: { value: "other.lan" } });
    expect(save).toBeEnabled();
    await act(async () => { fireEvent.click(save); });
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][0]).toMatchObject({ syslog_host: "other.lan" });
    await waitFor(() => expect(screen.getByRole("button", { name: "common.save" })).toBeDisabled());
    expect((screen.getByDisplayValue("other.lan") as HTMLInputElement).value).toBe("other.lan");
  });

  it("stays active when saving fails, so the change is not lost", async () => {
    update = vi.fn(() => Promise.reject(new Error("refused")));
    const save = await loaded();
    fireEvent.change(screen.getByDisplayValue("syslog.lan"), { target: { value: "bad" } });
    await act(async () => { fireEvent.click(save); });
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "common.save" })).toBeEnabled();
  });

  it("counts a switched option as a change", async () => {
    const save = await loaded();
    fireEvent.click(screen.getByRole("checkbox", { name: /auditExport.webhook/ }));
    expect(save).toBeEnabled();
  });
});
