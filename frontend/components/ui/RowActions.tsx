"use client";

import { ActionMenu, MenuAction } from "@/components/ui/ActionMenu";
import { IconButton } from "@/components/ui/IconButton";

/**
 * The one row-action pattern (docs/DESIGN-UI-2026-11.md section 6): at most `visible` (default two) icon buttons with a tooltip,
 * everything else in a "..." menu. Destructive actions are always in the menu, at the end, never next to the main actions.
 * Actions need an icon to be shown as a button; without one they go to the menu.
 */
export function RowActions({ actions, visible = 2 }: { actions: MenuAction[]; visible?: number }) {
  const direct = actions.filter((a) => a.tone !== "danger" && a.icon).slice(0, visible);
  const rest = actions.filter((a) => !direct.includes(a));
  return (
    <div className="flex items-center justify-end gap-1">
      {direct.map((a) =>
        a.href && !a.disabled
          ? <IconButton key={a.key} icon={a.icon!} label={a.label} href={a.href} {...(a.external ? { target: "_blank" } : {})} />
          : <IconButton key={a.key} icon={a.icon!} label={a.label} disabled={a.disabled} onClick={a.onSelect} />
      )}
      <ActionMenu actions={rest} />
    </div>
  );
}
