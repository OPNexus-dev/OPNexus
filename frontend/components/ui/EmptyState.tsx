import { LucideIcon } from "lucide-react";
import { ReactNode } from "react";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode;
}

/**
 * Consistent "nothing here yet" panel — replaces the ad hoc one-off empty
 * states each page previously wrote by hand (a stray <p>, a lone paragraph
 * in a table body, etc.) with one shared shape: icon, title, short
 * explanation, optional action (e.g. "Neu anlegen").
 */
export function EmptyState({ icon: Icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-slate-200 px-6 py-12 text-center">
      {Icon && <Icon className="mb-1 text-slate-400" size={28} strokeWidth={1.5} />}
      <p className="font-medium text-slate-700">{title}</p>
      {description && <p className="max-w-sm text-sm text-slate-500">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
