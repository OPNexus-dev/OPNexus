/**
 * Shortens a long value in the MIDDLE ("fe80::9618:82ff:fe11:2233" -> "fe80::9618…e11:2233"), so two long IPv6 addresses stay
 * distinguishable by their ends (cutting at the end made different addresses look the same). Short values are returned unchanged.
 */
export function shortenMiddle(value: string, max = 22): string {
  if (value.length <= max) return value;
  const keep = max - 1;
  const head = Math.ceil(keep * 0.55);
  const tail = keep - head;
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}
