import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageHeader } from "@/components/ui/PageHeader";

describe("PageHeader", () => {
  it("renders exactly one h1 with the title", () => {
    render(<PageHeader title="Firewall-Regeln" />);
    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(headings[0]).toHaveTextContent("Firewall-Regeln");
  });

  it("uses the standard size (20 px on phones, 24 px from sm)", () => {
    render(<PageHeader title="Hosts" />);
    const cls = screen.getByRole("heading", { level: 1 }).className;
    expect(cls).toContain("text-xl");
    expect(cls).toContain("sm:text-2xl");
  });

  it("adds no outer padding of its own, so every page starts at the same edge", () => {
    const { container } = render(<PageHeader title="Hosts" />);
    const header = container.querySelector("header")!;
    expect(header.className).not.toMatch(/\b(p|px|pl|pt)-\d/);
    expect(header.className).not.toContain("mx-auto");
  });

  it("shows actions and the tab row", () => {
    render(<PageHeader title="NAT" actions={<button>New rule</button>} tabs={<div role="tablist" />} />);
    expect(screen.getByRole("button", { name: "New rule" })).toBeInTheDocument();
    expect(screen.getByRole("tablist")).toBeInTheDocument();
  });

  it("renders the back link as an icon button with the label as its name", () => {
    render(<PageHeader title="test-fw-1" back={{ href: "/hosts", label: "Back to hosts" }} />);
    const link = screen.getByRole("link", { name: "Back to hosts" });
    expect(link).toHaveAttribute("href", "/hosts");
  });
});
