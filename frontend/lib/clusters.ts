import { Host } from "./api";

export function groupHostsByCluster(hosts: Host[]): { clusters: Record<string, Host[]>; ungrouped: Host[] } {
  const clusters: Record<string, Host[]> = {};
  const ungrouped: Host[] = [];
  for (const host of hosts) {
    if (host.cluster_name) {
      (clusters[host.cluster_name] ??= []).push(host);
    } else {
      ungrouped.push(host);
    }
  }
  return { clusters, ungrouped };
}
