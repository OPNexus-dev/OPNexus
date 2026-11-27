import { fireEvent, render, screen } from "@testing-library/react";
import { Pencil } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import { IconButton } from "@/components/ui/IconButton";

describe("IconButton", () => {
  it("has the label as its accessible name and does not submit forms", () => {
    render(<IconButton icon={Pencil} label="Edit rule" />);
    const button = screen.getByRole("button", { name: "Edit rule" });
    expect(button).toHaveAttribute("type", "button");
  });

  it("calls onClick and respects disabled", () => {
    const onClick = vi.fn();
    const { rerender } = render(<IconButton icon={Pencil} label="Edit" onClick={onClick} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    rerender(<IconButton icon={Pencil} label="Edit" onClick={onClick} disabled />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("is 32 px with a mouse and 44 px on touch (class contract)", () => {
    render(<IconButton icon={Pencil} label="Edit" />);
    const cls = screen.getByRole("button", { name: "Edit" }).className;
    expect(cls).toContain("h-8");
    expect(cls).toContain("[@media(pointer:coarse)]:h-11");
  });

  it("renders an external link with a safe rel when it opens a new tab", () => {
    render(<IconButton icon={Pencil} label="Open" href="https://10.0.0.1" target="_blank" />);
    const link = screen.getByRole("link", { name: "Open" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("uses the danger tone for destructive actions", () => {
    render(<IconButton icon={Pencil} label="Delete" tone="danger" />);
    expect(screen.getByRole("button", { name: "Delete" }).className).toContain("text-red-600");
  });
});
