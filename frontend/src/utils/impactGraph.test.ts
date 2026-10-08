import { describe, expect, it } from "vitest";
import { groupRelationGraph, ringLayout, subjectLink, treePositions } from "./impactGraph";
import type { RelationGraph, RelationNode } from "@/api/changeImpact";

const ROOT: RelationNode = { id: "ip_address:r", type: "ip_address", label: "192.0.2.5", impact: null, root: true };

function dns(i: number, impact = "change_required"): RelationNode {
  return { id: `dns_record:${i}`, type: "dns_record", label: `h${i}.example.com A 192.0.2.5`, impact, root: false,
           category: "dns" };
}
function graph(nodes: RelationNode[], edges: RelationGraph["edges"]): RelationGraph {
  return { nodes: [ROOT, ...nodes], edges, truncated: false, total_nodes: nodes.length + 1, limit: 100 };
}
const ref = (from: string, to = ROOT.id, relation = "references", strength = "exact") => ({ from, to, relation, strength });

describe("groupRelationGraph", () => {
  it("collapses many leaves of the same kind hanging off one node into a single group", () => {
    const leaves = Array.from({ length: 24 }, (_v, i) => dns(i));
    const g = groupRelationGraph(graph(leaves, leaves.map((n) => ref(n.id))));
    expect(g.nodes.map((n) => n.id)).toHaveLength(2);
    const grp = g.nodes.find((n) => n.group)!;
    expect(grp.count).toBe(24);
    expect(grp.category).toBe("dns");
    expect(grp.impact).toBe("change_required");
    expect(g.groups[grp.id].members).toHaveLength(24);
    // 群組只留一條邊，關係與強度沿用成員的
    expect(g.edges).toEqual([{ id: expect.any(String), from: grp.id, to: ROOT.id, relation: "references", strength: "exact",
                               propagates: false }]);
  });

  it("groups by kind across impacts and relations; the group takes the most severe impact", () => {
    // 使用者 2026-10-08：同類的分成好幾小群（影響或關係不同）反而看不出是什麼，一類一組就好
    const a = Array.from({ length: 2 }, (_v, i) => dns(i, "reference_only"));
    const b = [dns(10, "change_required")];
    const edges = [...a.map((n) => ref(n.id)), ...b.map((n) => ref(n.id, ROOT.id, "observed_on"))];
    const g = groupRelationGraph(graph([...a, ...b], edges));
    const grp = g.nodes.find((n) => n.group)!;
    expect(grp.count).toBe(3);
    expect(grp.impact).toBe("change_required");
    expect(g.groups[grp.id].relation).toBe("mixed");
  });

  it("drops self loops and marks which edges carry an outage", () => {
    const vm: RelationNode = { id: "virtual_machine:v", type: "virtual_machine", label: "app-01", impact: "modeled_disruption",
                               root: false, category: "workload" };
    const g = groupRelationGraph(graph([vm], [ref(ROOT.id, ROOT.id, "observed_on"), ref(vm.id, ROOT.id, "hosted_on")]));
    expect(g.edges).toHaveLength(1);
    expect(g.edges[0].propagates).toBe(true);
    const g2 = groupRelationGraph(graph([dns(1)], [ref("dns_record:1")]));
    expect(g2.edges[0].propagates).toBe(false);
  });

  it("leaves small sets and nodes on a path alone", () => {
    const two = [dns(1), dns(2)];
    const vm: RelationNode = { id: "virtual_machine:v", type: "virtual_machine", label: "app-01", impact: "modeled_disruption",
                               root: false, category: "workload" };
    const svc: RelationNode = { id: "service:s", type: "service", label: "ERP", impact: "modeled_disruption", root: false,
                                category: "service" };
    const g = groupRelationGraph(graph([...two, vm, svc], [
      ...two.map((n) => ref(n.id)),
      ref(vm.id, ROOT.id, "hosted_on"),
      ref(svc.id, vm.id, "requires_service"),
    ]));
    expect(g.nodes.some((n) => n.group)).toBe(false);
    expect(g.nodes).toHaveLength(5);
    expect(g.edges).toHaveLength(4);
  });

  it("expands one group or everything on request", () => {
    const leaves = Array.from({ length: 5 }, (_v, i) => dns(i));
    const data = graph(leaves, leaves.map((n) => ref(n.id)));
    const key = groupRelationGraph(data).nodes.find((n) => n.group)!.id;
    expect(groupRelationGraph(data, { expanded: new Set([key]) }).nodes).toHaveLength(6);
    expect(groupRelationGraph(data, { expandAll: true }).nodes).toHaveLength(6);
  });

  it("never groups the root", () => {
    const g = groupRelationGraph(graph([], []));
    expect(g.nodes).toEqual([expect.objectContaining({ id: ROOT.id, root: true, group: false })]);
  });
});

describe("treePositions", () => {
  const order = (a: string, b: string) => a.localeCompare(b);
  it("lays the tree out left to right with parents centred on their children", () => {
    const pos = treePositions(["r", "a", "b", "c", "a1", "a2"],
      [{ from: "a", to: "r" }, { from: "b", to: "r" }, { from: "c", to: "r" }, { from: "a1", to: "a" }, { from: "a2", to: "a" }],
      "r", order, { colW: 100, rowH: 10 });
    expect(pos.get("r")!.x).toBe(0);
    expect(pos.get("a")!.x).toBe(100);
    expect(pos.get("a1")!.x).toBe(200);
    // a 的兩個子節點在第 0、1 列，a 對齊中間；b、c 接著往下
    expect([pos.get("a1")!.y, pos.get("a2")!.y, pos.get("a")!.y]).toEqual([0, 10, 5]);
    expect([pos.get("b")!.y, pos.get("c")!.y]).toEqual([20, 30]);
    expect(pos.get("r")!.y).toBe((5 + 30) / 2);
  });
  it("places nodes that are not connected to the root below, and survives cycles", () => {
    const pos = treePositions(["r", "a", "x"], [{ from: "a", to: "r" }, { from: "r", to: "a" }, { from: "x", to: "x" }],
      "r", order, { colW: 100, rowH: 10 });
    expect(pos.size).toBe(3);
    expect(pos.get("x")!.y).toBeGreaterThan(pos.get("a")!.y);
  });
});

describe("ringLayout (by impact, one ring per impact)", () => {
  const levels = [
    { key: "potential_disruption", ids: ["p1", "p2"] },
    { key: "change_required", ids: ["c1", "c2", "c3", "c4"] },
    { key: "reference_only", ids: Array.from({ length: 12 }, (_, i) => `r${i}`) },
  ];
  it("puts the root in the middle and more severe impacts on inner rings", () => {
    const { positions, rings } = ringLayout(levels, "root");
    expect(positions.get("root")).toEqual({ x: 0, y: 0 });
    expect(rings.map((r) => r.key)).toEqual(["potential_disruption", "change_required", "reference_only"]);
    expect(rings[0].r).toBeLessThan(rings[1].r);
    expect(rings[1].r).toBeLessThan(rings[2].r);
    const dist = (id: string) => Math.hypot(positions.get(id)!.x, positions.get(id)!.y);
    expect(Math.abs(dist("p1") - rings[0].r)).toBeLessThan(1);
    expect(Math.abs(dist("r5") - rings[2].r)).toBeLessThan(1);
    expect(rings.map((r) => r.count)).toEqual([2, 4, 12]);
  });
  it("keeps neighbours on a ring at least a label width apart (big rings grow instead of crowding)", () => {
    const { positions } = ringLayout(levels, "root", { minArc: 170 });
    const ring = levels[2].ids.map((id) => positions.get(id)!);
    for (let i = 0; i < ring.length; i++) {
      const a = ring[i], b = ring[(i + 1) % ring.length];
      expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThan(150);
    }
  });
  it("never puts a node at the very top, where the ring title sits (no cross-shaped layout)", () => {
    const { positions, rings } = ringLayout(levels, "root");
    for (const [id, p] of positions) {
      if (id === "root") continue;
      const r = rings.find((x) => Math.abs(Math.hypot(p.x, p.y) - x.r) < 1)!;
      expect(Math.abs(p.x) < 1 && p.y < 0 && Math.abs(p.y + r.r) < 1).toBe(false);
    }
  });
  it("skips empty levels", () => {
    expect(ringLayout([{ key: "a", ids: [] }, { key: "b", ids: ["x"] }], null).rings.map((r) => r.key)).toEqual(["b"]);
  });
});

describe("subjectLink (open the original object from the graph panel)", () => {
  it("links records by id and integrations to their settings page", () => {
    expect(subjectLink({ type: "device", subject_id: "d1" })).toEqual({ name: "device-detail", params: { id: "d1" } });
    expect(subjectLink({ type: "ip_address", subject_id: "i1" })).toEqual({ name: "addresses", query: { open: "i1" } });
    expect(subjectLink({ type: "integration", kind: "proxmox" })).toEqual({ name: "virt_admin" });
    expect(subjectLink({ type: "integration", kind: "checkpoint" })).toEqual({ name: "checkpoint" });
  });
  it("gives no link when it cannot point somewhere real", () => {
    expect(subjectLink({ type: "device" })).toBeNull();
    expect(subjectLink({ type: "integration", kind: "nope" })).toBeNull();
    expect(subjectLink({ type: "fw_rule", subject_id: "r1" })).toBeNull();
  });
});

describe("ringLayout stagger", () => {
  it("rotates each ring a little so nodes on neighbouring rings do not line up", () => {
    const { positions } = ringLayout([{ key: "a", ids: ["a1", "a2", "a3", "a4"] }, { key: "b", ids: ["b1", "b2", "b3", "b4"] }], "r");
    const ang = (id: string) => Math.atan2(positions.get(id)!.y, positions.get(id)!.x);
    const diffs = ["b1", "b2", "b3", "b4"].map((b) => Math.min(...["a1", "a2", "a3", "a4"].map((a) => {
      const d = Math.abs(ang(a) - ang(b)) % (2 * Math.PI);
      return Math.min(d, 2 * Math.PI - d);
    })));
    expect(Math.min(...diffs)).toBeGreaterThan(0.15);
  });
});
