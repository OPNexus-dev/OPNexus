import clsx from "clsx";
import { useRef } from "react";

export interface TabItem {
  key: string;
  label: React.ReactNode;
}

interface TabsProps {
  items: TabItem[];
  active: string;
  onChange: (key: string) => void;
  className?: string;
}

/**
 * Accessible tab strip (role="tablist"/"tab", arrow-key navigation) matching the
 * existing hand-rolled TabButton visual style used across rules/nat/logs/etc —
 * a drop-in replacement for that pattern with real ARIA semantics and keyboard
 * support, which the ad hoc version never had.
 */
export function Tabs({ items, active, onChange, className }: TabsProps) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  function onKeyDown(e: React.KeyboardEvent, index: number) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const dir = e.key === "ArrowRight" ? 1 : -1;
    const next = items[(index + dir + items.length) % items.length];
    onChange(next.key);
    refs.current[next.key]?.focus();
  }

  return (
    <div role="tablist" className={clsx("flex gap-1 border-b border-slate-200", className)}>
      {items.map((item, index) => {
        const isActive = item.key === active;
        return (
          <button
            key={item.key}
            ref={(el) => { refs.current[item.key] = el; }}
            role="tab"
            aria-selected={isActive}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onChange(item.key)}
            onKeyDown={(e) => onKeyDown(e, index)}
            className={clsx(
              "border-b-2 px-3 py-2 text-sm font-medium transition-colors",
              isActive ? "border-brand-500 text-brand-600" : "border-transparent text-slate-500 hover:text-slate-700"
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
