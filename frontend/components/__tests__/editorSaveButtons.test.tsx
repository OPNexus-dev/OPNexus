import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: {
    getHostVariables: () => Promise.resolve({ variables: [
      { name: "LAN_NET", value: "192.168.1.0/24", used_by: [], missing: false, role: false },
    ] }),
    getHostInterfaces: () => Promise.resolve([]),
    setHostVariables: (_id: string, variables: Record<string, string>) => Promise.resolve({ variables: Object.entries(variables).map(([name, value]) => ({ name, value, used_by: [], missing: false, role: false })) }),
  },
}));
vi.mock("@/lib/useReadOnly", () => ({ useReadOnly: () => false, useNotAdmin: () => false }));

import { HostVariables } from "@/components/HostVariables";
import { MaintenanceWindowEditor } from "@/components/MaintenanceWindowEditor";
import type { Host, MaintenanceWindow } from "@/lib/api";

const windowItem = {
  id: "w1", name: "Night", timezone: "Europe/Berlin", recurrence: "weekly", weekdays: [6], month_mode: "nth_weekday", month_nth: 1, month_weekday: 0, month_day: 1,
  start_time: "02:00", duration_minutes: 120, start_cutoff_minutes: 30, enabled: true,
  created_by: "x", created_at: "", open_now: false, next: [], exceptions: [], used_by: [],
} as unknown as MaintenanceWindow;

describe("Save button of an edit dialog", () => {
  it("maintenance window: grey while unchanged, active after an edit, grey again when undone", () => {
    render(<MaintenanceWindowEditor window={windowItem} onClose={() => {}} onSaved={() => {}} />);
    const save = screen.getByRole("button", { name: "rollouts.plan.save" });
    expect(save).toBeDisabled();
    const name = screen.getByDisplayValue("Night");
    fireEvent.change(name, { target: { value: "Night 2" } });
    expect(save).toBeEnabled();
    fireEvent.change(name, { target: { value: "Night" } });
    expect(save).toBeDisabled();
  });

  it("maintenance window: a NEW window is not held back (it only needs a name)", () => {
    render(<MaintenanceWindowEditor window={null} onClose={() => {}} onSaved={() => {}} />);
    const save = screen.getByRole("button", { name: "rollouts.plan.save" });
    expect(save).toBeDisabled();   // no name yet
    fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "Weekend" } });
    expect(save).toBeEnabled();
  });
});

describe("Save button of the host variables", () => {
  it("is grey after loading, active after a value change, grey after saving", async () => {
    render(<HostVariables host={{ id: "h1", name: "fw" } as Host} />);
    const value = await screen.findByDisplayValue("192.168.1.0/24");
    const save = screen.getAllByRole("button", { name: "common.save" })[0];
    expect(save).toBeDisabled();
    fireEvent.change(value, { target: { value: "10.0.0.0/8" } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "common.save" })[0]).toBeDisabled());
    expect((screen.getByDisplayValue("10.0.0.0/8") as HTMLInputElement).value).toBe("10.0.0.0/8");
  });
});
