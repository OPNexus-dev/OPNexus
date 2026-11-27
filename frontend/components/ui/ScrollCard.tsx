"use client";

import clsx from "clsx";
import { HTMLAttributes } from "react";
import { Card } from "@/components/ui/Card";
import { ScrollRow } from "@/components/ui/ScrollRow";

/**
 * A card around content that can be wider than the screen (a table that is not a DataTable, a log, a diff): it scrolls sideways,
 * shows an edge fade where there is more, and can be reached by keyboard. Replaces `<Card className="overflow-x-auto">`, which cut content off silently.
 */
export function ScrollCard({ className, children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <Card className={clsx("overflow-hidden", className)} {...props}>
      <ScrollRow layout="block" fadeFrom="from-[rgb(var(--surface))]">{children}</ScrollRow>
    </Card>
  );
}
