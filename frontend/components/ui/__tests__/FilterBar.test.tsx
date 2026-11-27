import "@/test/viewport";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { setViewport } from "@/test/viewport";
import { FilterBar } from "@/components/ui/FilterBar";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { StatChip } from "@/components/ui/StatChip";

function Harness({ onReset }: { onReset?: () => void }) {
  const [q, setQ] = useState("");
  const [state, setState] = useState("");
  const [use, setUse] = useState("");
  return (
    <FilterBar
      search={{ value: q, onChange: setQ, placeholder: "Name, source …", label: "Search rules" }}
      filters={[
        { key: "state", label: "State", value: state, onChange: setState, allLabel: "All states", options: [{ value: "active", label: "Active" }, { value: "draft", label: "Draft" }] },
        { key: "use", label: "Usage", value: use, onChange: setUse, allLabel: "Any usage", options: [{ value: "unused", label: "Unused" }] },
      ]}
      onReset={() => { setQ(""); setState(""); setUse(""); onReset?.(); }}
      trailing={<StatChip count={2} label="pending" tone="warning" />}
    />
  );
}

describe("FilterBar on a desktop", () => {
  it("shows the search and all filters inline, no sheet button", () => {
    setViewport(1440);
    render(<Harness />);
    expect(screen.getByRole("searchbox", { name: "Search rules" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "State" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Usage" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "common.filter" })).toBeNull();
  });

  it("shows active filters as removable chips and a reset", () => {
    setViewport(1440);
    render(<Harness />);
    expect(screen.queryByRole("button", { name: /common.resetFilters/ })).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "State" }), { target: { value: "draft" } });
    expect(screen.getByText("State: Draft")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /common.removeFilter/ }));
    expect(screen.queryByText("State: Draft")).toBeNull();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "dns" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Usage" }), { target: { value: "unused" } });
    fireEvent.click(screen.getByRole("button", { name: "common.resetFilters" }));
    expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("combobox", { name: "Usage" }) as HTMLSelectElement).value).toBe("");
  });

  it("keeps the counter chips in the toolbar", () => {
    setViewport(1440);
    render(<Harness />);
    expect(screen.getByText("pending")).toBeInTheDocument();
  });
});

describe("FilterBar on a phone", () => {
  it("shows only the search and one Filter button; the filters are in a sheet", () => {
    setViewport(390);
    render(<Harness />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "common.filter" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByRole("combobox")).toHaveLength(2);
  });

  it("the button counts the active filters and the sheet closes with Done", () => {
    setViewport(390);
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "common.filter" }));
    fireEvent.change(within(screen.getByRole("dialog")).getByRole("combobox", { name: "State" }), { target: { value: "active" } });
    fireEvent.click(screen.getByRole("button", { name: "common.done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "common.filterCount" })).toBeInTheDocument();
  });
});

describe("StatChip", () => {
  it("shows a zero in the neutral tone without fading (fading breaks the contrast) and a non-zero count in its tone", () => {
    const { rerender } = render(<StatChip count={0} label="to review" tone="warning" />);
    const zero = screen.getByText("to review").parentElement!;
    expect(zero.className).toContain("tone-neutral");
    expect(zero.className).not.toContain("opacity");
    rerender(<StatChip count={3} label="to review" tone="warning" />);
    expect(screen.getByText("to review").parentElement!.className).toContain("tone-warning");
  });

  it("is a button only when it has an action, and reports its pressed state", () => {
    const onClick = vi.fn();
    const { rerender } = render(<StatChip count={1} label="pending" />);
    expect(screen.queryByRole("button")).toBeNull();
    rerender(<StatChip count={1} label="pending" onClick={onClick} active />);
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalled();
  });
});

describe("SegmentedControl", () => {
  it("is a named radio group; choosing calls onChange", () => {
    const onChange = vi.fn();
    render(<SegmentedControl label="View" value="table" onChange={onChange} options={[{ value: "table", label: "Table" }, { value: "cards", label: "Cards" }]} />);
    expect(screen.getByRole("group", { name: "View" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Table" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Cards" }));
    expect(onChange).toHaveBeenCalledWith("cards");
  });
});
