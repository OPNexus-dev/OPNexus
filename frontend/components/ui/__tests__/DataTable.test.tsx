import "@/test/viewport";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { Pencil, Trash2 } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import { setViewport } from "@/test/viewport";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import { Column, DataTable, sortRows } from "@/components/ui/DataTable";

interface Row { id: string; name: string; src: string; dst: string; state: string; hit: number | null; note: string }
const rows: Row[] = [
  { id: "1", name: "Block telnet", src: "10.0.0.0/8", dst: "any:23", state: "active", hit: 5, note: "old" },
  { id: "2", name: "Allow DNS", src: "any", dst: "10.0.0.53:53", state: "draft", hit: null, note: "new" },
  { id: "3", name: "allow ntp", src: "LAN net", dst: "any:123", state: "active", hit: 2, note: "x" },
];
const columns: Column<Row>[] = [
  { key: "name", header: "Rule", priority: "primary", sortable: true, sortValue: (r) => r.name, cell: (r) => r.name },
  { key: "state", header: "Status", priority: "primary", cell: (r) => <span>{r.state}</span> },
  { key: "src", header: "Source", priority: "high", mono: true, cell: (r) => r.src },
  { key: "dst", header: "Destination", priority: "high", mono: true, cell: (r) => r.dst },
  { key: "hit", header: "Last hit", priority: "medium", sortable: true, sortValue: (r) => r.hit, cell: (r) => (r.hit === null ? "—" : String(r.hit)) },
  { key: "note", header: "Note", priority: "low", cell: (r) => r.note },
];
const base = { rows, columns, rowKey: (r: Row) => r.id, label: "Rules" };

describe("sortRows", () => {
  it("sorts text with the locale, numbers numerically, empty values last in both directions", () => {
    expect(sortRows(rows, columns, { key: "name", dir: "asc" }, "en").map((r) => r.id)).toEqual(["2", "3", "1"]);
    expect(sortRows(rows, columns, { key: "hit", dir: "asc" }, "en").map((r) => r.id)).toEqual(["3", "1", "2"]);
    expect(sortRows(rows, columns, { key: "hit", dir: "desc" }, "en").map((r) => r.id)).toEqual(["1", "3", "2"]);
  });
  it("returns the rows untouched without sort or without a sort value", () => {
    expect(sortRows(rows, columns, null, "en")).toBe(rows);
    expect(sortRows(rows, columns, { key: "src", dir: "asc" }, "en")).toBe(rows);
  });
});

describe("DataTable on a desktop", () => {
  it("renders a real table with all columns and one row per entry", () => {
    setViewport(1440);
    render(<DataTable {...base} />);
    const table = screen.getByRole("table", { name: "Rules" });
    expect(within(table).getAllByRole("columnheader")).toHaveLength(6);
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });

  it("makes the first primary column the row header", () => {
    setViewport(1440);
    render(<DataTable {...base} />);
    expect(screen.getByRole("rowheader", { name: "Block telnet" })).toBeInTheDocument();
  });

  it("sorts by clicking a sortable header and reports aria-sort", () => {
    setViewport(1440);
    render(<DataTable {...base} />);
    const header = screen.getByRole("columnheader", { name: /Rule/ });
    expect(header).toHaveAttribute("aria-sort", "none");
    fireEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(screen.getAllByRole("rowheader").map((c) => c.textContent)).toEqual(["Allow DNS", "allow ntp", "Block telnet"]);
    fireEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "descending");
    fireEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "none");
  });

  it("in server mode the parent sorts: the table only reports the click", () => {
    setViewport(1440);
    const onSortChange = vi.fn();
    render(<DataTable {...base} sort={{ key: "name", dir: "desc" }} onSortChange={onSortChange} sortMode="server" />);
    expect(screen.getAllByRole("rowheader").map((c) => c.textContent)).toEqual(["Block telnet", "Allow DNS", "allow ntp"]);
    fireEvent.click(within(screen.getByRole("columnheader", { name: /Rule/ })).getByRole("button"));
    expect(onSortChange).toHaveBeenCalledWith(null);
  });

  it("has no clickable rows: interactive elements are separate controls (axe nested-interactive)", () => {
    setViewport(1440);
    const { container } = render(<DataTable {...base} actions={() => [{ key: "e", label: "Edit", icon: Pencil, onSelect: () => {} }]} />);
    for (const tr of container.querySelectorAll("tbody tr")) {
      expect(tr.getAttribute("onclick")).toBeNull();
      expect(tr.getAttribute("role")).toBeNull();
      expect(tr.getAttribute("tabindex")).toBeNull();
    }
  });

  it("shows the empty state instead of an empty table", () => {
    setViewport(1440);
    render(<DataTable {...base} rows={[]} empty={<p>Nothing yet</p>} />);
    expect(screen.getByText("Nothing yet")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("DataTable selection", () => {
  it("selects all, shows the indeterminate state and toggles single rows", () => {
    setViewport(1440);
    const onChange = vi.fn();
    const { rerender } = render(<DataTable {...base} selection={{ selected: new Set(), onChange, rowLabel: (r) => `Select ${r.name}` }} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "common.selectAll" }));
    expect([...onChange.mock.calls[0][0]].sort()).toEqual(["1", "2", "3"]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Allow DNS" }));
    expect([...onChange.mock.calls[1][0]]).toEqual(["2"]);
    rerender(<DataTable {...base} selection={{ selected: new Set(["1"]), onChange, rowLabel: (r) => `Select ${r.name}` }} />);
    expect((screen.getByRole("checkbox", { name: "common.selectAll" }) as HTMLInputElement).indeterminate).toBe(true);
    rerender(<DataTable {...base} selection={{ selected: new Set(["1", "2", "3"]), onChange, rowLabel: (r) => `Select ${r.name}` }} />);
    expect((screen.getByRole("checkbox", { name: "common.selectAll" }) as HTMLInputElement).checked).toBe(true);
  });
});

describe("DataTable on a tablet", () => {
  it("shows primary and high columns and puts the others behind a details toggle", () => {
    setViewport(820);
    render(<DataTable {...base} />);
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent?.trim());
    expect(headers).toContain("Rule");
    expect(headers).not.toContain("Last hit");
    expect(headers).not.toContain("Note");
    const toggle = screen.getAllByRole("button", { name: "common.showDetails" })[0];
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Last hit")).toBeInTheDocument();
    expect(screen.getByText("Note")).toBeInTheDocument();
  });
});

describe("DataTable on a phone", () => {
  it("renders cards, not a table: title, badge, labelled values, nothing cut off", () => {
    setViewport(390);
    render(<DataTable {...base} />);
    expect(screen.queryByRole("table")).toBeNull();
    const list = screen.getByRole("list", { name: "Rules" });
    const cards = within(list).getAllByRole("listitem");
    expect(cards).toHaveLength(3);
    const first = cards[0];
    expect(within(first).getByText("Block telnet")).toBeInTheDocument();
    expect(within(first).getByText("active")).toBeInTheDocument();
    expect(within(first).getByText("Source")).toBeInTheDocument();
    expect(within(first).getByText("10.0.0.0/8")).toBeInTheDocument();
    expect(within(first).queryByText("Note")).toBeNull();
  });

  it("keeps medium and low columns reachable through Details", () => {
    setViewport(390);
    render(<DataTable {...base} />);
    const first = screen.getAllByRole("listitem")[0];
    fireEvent.click(within(first).getByRole("button", { name: "common.showDetails" }));
    expect(within(first).getByText("Last hit")).toBeInTheDocument();
    expect(within(first).getByText("old")).toBeInTheDocument();
  });

  it("every card has its row actions, with the destructive one in the menu", () => {
    setViewport(390);
    render(<DataTable {...base} actions={(r) => [
      { key: "edit", label: `Edit ${r.name}`, icon: Pencil, onSelect: () => {} },
      { key: "del", label: `Delete ${r.name}`, icon: Trash2, tone: "danger", onSelect: () => {} },
    ]} />);
    const first = screen.getAllByRole("listitem")[0];
    expect(within(first).getByRole("button", { name: "Edit Block telnet" })).toBeInTheDocument();
    expect(within(first).queryByRole("button", { name: "Delete Block telnet" })).toBeNull();
    expect(within(first).getByRole("button", { name: "common.moreActions" })).toBeInTheDocument();
  });

  it("offers sorting as a select (a header row does not exist on cards)", () => {
    setViewport(390);
    render(<DataTable {...base} />);
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "name:asc" } });
    const titles = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(titles[0]).toContain("Allow DNS");
  });

  it("list mode is compact and has no card chrome", () => {
    setViewport(390);
    render(<DataTable {...base} mobile="list" />);
    const list = screen.getByRole("list", { name: "Rules" });
    expect(list.className).toContain("divide-y");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("scroll mode keeps the table, labelled and focusable where it scrolls", () => {
    setViewport(390);
    const { container } = render(<DataTable {...base} mobile="scroll" />);
    expect(screen.getByRole("table", { name: "Rules" })).toBeInTheDocument();
    const region = container.querySelector("div.overflow-x-auto")!;
    Object.defineProperty(region, "scrollWidth", { configurable: true, value: 900 });
    Object.defineProperty(region, "clientWidth", { configurable: true, value: 300 });
    act(() => { region.dispatchEvent(new Event("scroll")); });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveAttribute("role", "region");
    expect(region).toHaveAttribute("aria-label", "Rules");
    expect(container.querySelector('[data-fade="right"]')).not.toBeNull();
  });

  it("selection on cards has one select-all above the list and a checkbox per card", () => {
    setViewport(390);
    const onChange = vi.fn();
    render(<DataTable {...base} selection={{ selected: new Set(), onChange, rowLabel: (r) => `Select ${r.name}` }} />);
    expect(screen.getAllByRole("checkbox", { name: /Select (Block|Allow|allow)/ })).toHaveLength(3);
    expect(screen.getAllByText("common.selectAll")).toHaveLength(1);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Block telnet" }));
    expect([...onChange.mock.calls[0][0]]).toEqual(["1"]);
  });
});

describe("DataTable row detail", () => {
  const detail = { render: (r: Row) => <p>Steps of {r.name}</p> };

  it("desktop: a toggle per row opens the detail below it, rendered only while open", () => {
    setViewport(1440);
    render(<DataTable {...base} detail={detail} />);
    expect(screen.queryByText("Steps of Block telnet")).toBeNull();
    const toggles = screen.getAllByRole("button", { name: "common.showDetails" });
    expect(toggles).toHaveLength(3);
    fireEvent.click(toggles[0]);
    expect(screen.getByText("Steps of Block telnet")).toBeInTheDocument();
    expect(screen.queryByText("Steps of Allow DNS")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "common.hideDetails" }));
    expect(screen.queryByText("Steps of Block telnet")).toBeNull();
  });

  it("tablet: the detail comes after the columns that did not fit", () => {
    setViewport(820);
    render(<DataTable {...base} detail={detail} />);
    fireEvent.click(screen.getAllByRole("button", { name: "common.showDetails" })[0]);
    expect(screen.getByText("Last hit")).toBeInTheDocument();
    expect(screen.getByText("Steps of Block telnet")).toBeInTheDocument();
  });

  it("phone cards: Details shows the detail too", () => {
    setViewport(390);
    render(<DataTable {...base} detail={detail} />);
    const first = screen.getAllByRole("listitem")[0];
    fireEvent.click(within(first).getByRole("button", { name: "common.showDetails" }));
    expect(within(first).getByText("Steps of Block telnet")).toBeInTheDocument();
  });

  it("without detail and on a desktop there is no toggle", () => {
    setViewport(1440);
    render(<DataTable {...base} />);
    expect(screen.queryByRole("button", { name: "common.showDetails" })).toBeNull();
  });
});

describe("DataTable title column", () => {
  const cols: Column<Row>[] = [
    { key: "state", header: "Status", priority: "primary", cell: (r) => <span>{r.state}</span> },
    { key: "name", header: "Rule", priority: "primary", isTitle: true, cell: (r) => r.name },
    { key: "src", header: "Source", priority: "high", cell: (r) => r.src },
  ];
  it("uses the marked column as card title and as row header, whatever its position", () => {
    setViewport(1440);
    const { unmount } = render(<DataTable {...base} columns={cols} />);
    expect(screen.getByRole("rowheader", { name: "Block telnet" })).toBeInTheDocument();
    unmount();
    setViewport(390);
    render(<DataTable {...base} columns={cols} />);
    const first = screen.getAllByRole("listitem")[0];
    expect(first.querySelector(".font-medium")?.textContent).toBe("Block telnet");
  });
});

describe("DataTable actions column", () => {
  it("stays visible while the table scrolls sideways (sticky at the right edge)", () => {
    setViewport(820);
    const { container } = render(<DataTable {...base} actions={() => [{ key: "e", label: "Edit", icon: Pencil, onSelect: () => {} }]} />);
    const cell = container.querySelector("tbody tr td:last-child")!;
    expect(cell.className).toContain("sticky");
    expect(cell.className).toContain("right-0");
    expect(container.querySelector("thead th:last-child")!.className).toContain("sticky");
  });

  it("the edge fade uses the surface colour, so it also fits the dark theme", () => {
    setViewport(1440);
    const { container } = render(<DataTable {...base} />);
    const wrapper = container.querySelector("div.overflow-x-auto")!;
    Object.defineProperty(wrapper, "scrollWidth", { configurable: true, value: 900 });
    Object.defineProperty(wrapper, "clientWidth", { configurable: true, value: 300 });
    act(() => { wrapper.dispatchEvent(new Event("scroll")); });
    expect(container.querySelector('[data-fade="right"]')!.className).toContain("--surface");
  });
});

describe("DataTable compact list keeps every column reachable", () => {
  it("shows two columns on the item and puts the remaining ones behind Details", () => {
    setViewport(390);
    render(<DataTable {...base} mobile="list" />);
    const first = screen.getAllByRole("listitem")[0];
    expect(within(first).getByText("Source")).toBeInTheDocument();
    expect(within(first).getByText("Destination")).toBeInTheDocument();
    expect(within(first).queryByText("Last hit")).toBeNull();
    fireEvent.click(within(first).getByRole("button", { name: "common.showDetails" }));
    expect(within(first).getByText("Last hit")).toBeInTheDocument();
    expect(within(first).getByText("Note")).toBeInTheDocument();
  });
});

describe("DataTable scroll mode", () => {
  it("fixes the first column only when no selection or toggle column sits left of it", () => {
    setViewport(390);
    const { container, unmount } = render(<DataTable {...base} mobile="scroll" />);
    expect(container.querySelector("tbody tr > :first-child")!.className).toContain("sticky");
    unmount();
    const { container: c2 } = render(<DataTable {...base} mobile="scroll" selection={{ selected: new Set(), onChange: () => {}, rowLabel: (r) => r.name }} />);
    for (const cell of c2.querySelectorAll("tbody tr > *")) {
      if (!cell.querySelector("button")) expect(cell.className).not.toContain("left-0");
    }
  });
});

describe("DataTable bare", () => {
  it("drops its own frame so it can sit inside a card", () => {
    setViewport(1440);
    const { container, rerender } = render(<DataTable {...base} />);
    expect((container.firstChild as HTMLElement).className).toContain("border");
    rerender(<DataTable {...base} bare />);
    expect((container.firstChild as HTMLElement).className).not.toContain("border");
    expect((container.firstChild as HTMLElement).className).not.toContain("shadow");
  });
});

describe("DataTable cards without actions", () => {
  it("do not show an empty footer when a row has no actions", () => {
    setViewport(390);
    const { container } = render(<DataTable {...base} actions={(r) => (r.id === "1" ? [{ key: "e", label: "Edit", icon: Pencil, onSelect: () => {} }] : [])} />);
    const items = screen.getAllByRole("listitem");
    expect(items[0].querySelector(".border-t")).not.toBeNull();
    expect(items[1].querySelector(".border-t")).toBeNull();
    expect(container.querySelectorAll(".border-t")).toHaveLength(1);
  });
});
