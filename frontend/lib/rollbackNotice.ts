/**
 * Rollback notice for the rules page.
 *
 * A rule that is not confirmed within the confirm window is rolled back by the server in the background (sweep loop),
 * host by host (every host has its own deadline). For a NEW rule on a host that deletes the deployment row, so afterwards
 * nothing on the row says what happened: the status just falls back to "changes pending". These two helpers let the page
 * notice that a deployment that was waiting for confirmation has been taken back, so it can tell the user once.
 */
interface RuleLike {
  id: string;
  description: string;
  deployments: { host_id: string; host_name?: string; status: string }[];
}

/** What was waiting for confirmation: rule id -> description and the hosts (id -> name) with a pending deployment. */
export type PendingRules = Map<string, { description: string; hosts: Map<string, string> }>;

export function pendingRules(rules: RuleLike[]): PendingRules {
  const pending: PendingRules = new Map();
  for (const rule of rules) {
    const hosts = new Map<string, string>();
    for (const d of rule.deployments) if (d.status === "pending") hosts.set(d.host_id, d.host_name ?? d.host_id);
    if (hosts.size) pending.set(rule.id, { description: rule.description, hosts });
  }
  return pending;
}

export interface RolledBack {
  description: string;
  hosts: string[];
}

/**
 * Rules where a host that was pending in `before` has since been rolled back automatically: its deployment row is gone
 * (new rule on that host) or is "reverted" (edited rule). Not reported: a host that is still pending, was confirmed
 * ("success"), failed ("error"/"revert_failed": the row shows that already) or whose rule was deleted meanwhile.
 * Decided per host, so a rule that is live on host A and was rolled back on host B is reported too.
 */
export function rolledBackRules(before: PendingRules, rules: RuleLike[]): RolledBack[] {
  const out: RolledBack[] = [];
  for (const [id, entry] of before) {
    const rule = rules.find((r) => r.id === id);
    if (!rule) continue;
    const hosts: string[] = [];
    for (const [hostId, hostName] of entry.hosts) {
      const now = rule.deployments.find((d) => d.host_id === hostId);
      if (!now || now.status === "reverted") hosts.push(hostName);
    }
    if (hosts.length) out.push({ description: entry.description, hosts });
  }
  return out;
}
