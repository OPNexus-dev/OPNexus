import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Modal } from "@/components/ui/Modal";

describe("Modal", () => {
  it("is a named modal dialog and closes with Escape", () => {
    const onClose = vi.fn();
    render(<Modal title="Edit rule" onClose={onClose}><p>body</p></Modal>);
    expect(screen.getByRole("dialog", { name: "Edit rule" })).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is centred by default and sits at the bottom edge as a sheet on small screens when asked", () => {
    const { rerender, container } = render(<Modal title="A" onClose={() => {}}>x</Modal>);
    expect((container.firstChild as HTMLElement).className).toContain("items-center");
    expect((container.firstChild as HTMLElement).className).not.toContain("items-end");
    rerender(<Modal title="A" onClose={() => {}} sheet>x</Modal>);
    expect((container.firstChild as HTMLElement).className).toContain("items-end");
    expect(screen.getByRole("dialog").className).toContain("rounded-t-2xl");
  });
});
