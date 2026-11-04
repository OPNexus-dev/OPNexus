import type { InterfaceOption } from "@/lib/api";

// The protocols everyone needs, always first and always present - also when no firewall answered the live list.
// "TCP/UDP" is a real OPNsense choice (one rule for both, e.g. DNS on port 53); a plain "tcp" or "udp" is not enough there.
const COMMON: InterfaceOption[] = [
  { name: "any", label: "any" },
  { name: "TCP", label: "TCP" },
  { name: "UDP", label: "UDP" },
  { name: "TCP/UDP", label: "TCP/UDP" },
  { name: "ICMP", label: "ICMP" },
];

/** Fallback while the live list is not (yet) available. */
export const PROTOCOL_FALLBACK: InterfaceOption[] = COMMON;

/**
 * The options for a protocol select: the common ones first, then the rest of the live list alphabetically.
 * A stored value that is not in the list (a different spelling such as "tcp", or a protocol this firewall lacks) stays selectable.
 * OPNsense treats the spelling of tcp/udp/icmp case-insensitively, so "tcp" is shown as the entry "TCP" instead of a duplicate.
 */
export function protocolSelect(live: InterfaceOption[], current: string): { options: InterfaceOption[]; value: string } {
  const lower = (s: string) => s.toLowerCase();
  const common = new Set(COMMON.map((o) => lower(o.name)));
  const rest = live.filter((o) => !common.has(lower(o.name)));
  const options = [...COMMON, ...rest];
  const match = options.find((o) => lower(o.name) === lower(current));
  if (match) return { options, value: match.name };
  return { options: [{ name: current, label: current }, ...options], value: current };
}
