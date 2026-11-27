import { describe, expect, it } from "vitest";
import { pendingRules, rolledBackRules } from "../rollbackNotice";

// deployments as [host, status] pairs
const rule = (id: string, description: string, ...deployments: [string, string][]) => ({
  id,
  description,
  deployments: deployments.map(([host, status]) => ({ host_id: host, host_name: `fw-${host}`, status })),
});

describe("pendingRules", () => {
  it("lists only rules with a deployment waiting for confirmation, with those hosts", () => {
    const map = pendingRules([rule("1", "a", ["A", "pending"], ["B", "success"]), rule("2", "b", ["A", "success"]), rule("3", "c")]);
    expect([...map.keys()]).toEqual(["1"]);
    expect([...map.get("1")!.hosts.entries()]).toEqual([["A", "fw-A"]]);
  });
});

describe("rolledBackRules", () => {
  const before = pendingRules([rule("1", "Block guests", ["A", "pending"])]);

  it("reports a new rule whose deployment rows are gone after the rollback", () => {
    expect(rolledBackRules(before, [rule("1", "Block guests")])).toEqual([{ description: "Block guests", hosts: ["fw-A"] }]);
  });
  it("reports an edited rule whose deployment is reverted", () => {
    expect(rolledBackRules(before, [rule("1", "Block guests", ["A", "reverted"])])).toHaveLength(1);
  });
  it("is silent while the rule is still pending", () => {
    expect(rolledBackRules(before, [rule("1", "Block guests", ["A", "pending"])])).toEqual([]);
  });
  it("is silent when the rule was confirmed", () => {
    expect(rolledBackRules(before, [rule("1", "Block guests", ["A", "success"])])).toEqual([]);
  });
  it("is silent when the rollback itself failed or the deployment errored (the row already shows that)", () => {
    expect(rolledBackRules(before, [rule("1", "Block guests", ["A", "revert_failed"])])).toEqual([]);
    expect(rolledBackRules(before, [rule("1", "Block guests", ["A", "error"])])).toEqual([]);
  });
  it("is silent when the rule was deleted meanwhile", () => {
    expect(rolledBackRules(before, [])).toEqual([]);
  });
  it("only reports rules that were pending before", () => {
    expect(rolledBackRules(new Map(), [rule("1", "Block guests")])).toEqual([]);
  });

  // The cases that a whole-rule check missed: a rule that stays live on one host while another host is rolled back.
  it("reports a host that was added to a live rule and not confirmed (rule stays live on the other host)", () => {
    const b = pendingRules([rule("1", "R", ["A", "success"], ["B", "pending"])]);
    expect(rolledBackRules(b, [rule("1", "R", ["A", "success"])])).toEqual([{ description: "R", hosts: ["fw-B"] }]);
  });
  it("reports the host whose deadline passed even though the other host was confirmed", () => {
    const b = pendingRules([rule("1", "R", ["A", "pending"], ["B", "pending"])]);
    expect(rolledBackRules(b, [rule("1", "R", ["B", "success"])])).toEqual([{ description: "R", hosts: ["fw-A"] }]);
  });
  it("names only the hosts that were rolled back, not those still pending", () => {
    const b = pendingRules([rule("1", "R", ["A", "pending"], ["B", "pending"])]);
    expect(rolledBackRules(b, [rule("1", "R", ["B", "pending"])])).toEqual([{ description: "R", hosts: ["fw-A"] }]);
  });
  it("is silent when a host fell from pending to error (not a clean rollback)", () => {
    const b = pendingRules([rule("1", "R", ["A", "pending"])]);
    expect(rolledBackRules(b, [rule("1", "R", ["A", "error"])])).toEqual([]);
  });
});
