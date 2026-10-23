import { describe, expect, it } from "vitest";
import { arpLines, fdbBlocks } from "../bleedEvidence";

describe("bleedEvidence", () => {
  it("pairs two addresses that answer with each other's MAC", () => {
    const lines = arpLines([
      { ip: "198.51.100.68", mac: "bc:24:11:00:00:01", mac_owner_ip: "192.0.2.68" },
      { ip: "192.0.2.82", mac: "bc:24:11:00:00:02", mac_owner_ip: "198.51.100.82" },
      { ip: "192.0.2.68", mac: "bc:24:11:00:00:03", mac_owner_ip: "198.51.100.68" },
      { ip: "192.0.2.68", mac: "bc:24:11:00:00:03", mac_owner_ip: "198.51.100.68" },   // 重複
    ]);
    expect(lines).toHaveLength(2);
    expect(lines[0]).toEqual({ kind: "pair", a: "198.51.100.68", b: "192.0.2.68",
                               macA: "bc:24:11:00:00:03", macB: "bc:24:11:00:00:01" });
    expect(lines[1]).toEqual({ kind: "one", ip: "192.0.2.82", mac: "bc:24:11:00:00:02", owner: "198.51.100.82" });
  });

  it("merges blocks of the same switch and VLAN and flags ports carrying both subnets", () => {
    const blocks = fdbBlocks([
      { switch: "sw-a", vlan: 1, macs: { "192.0.2.0/24": [{ port: "Eth1/1", mac: "m1" }, { port: "Eth1/1", mac: "m2" }],
                                          "198.51.100.0/24": [{ port: "Eth1/3", mac: "m3" }] } },
      { switch: "sw-a", vlan: 1, macs: { "198.51.100.0/24": [{ port: "Eth1/1", mac: "m4" }] } },
      { switch: "sw-b", vlan: 1, macs: { "192.0.2.0/24": [{ port: "eth0/2", mac: "m5" }] } },
    ]);
    expect(blocks).toHaveLength(2);
    const a = blocks[0];
    expect(a.subnets.map((s) => s.cidr)).toEqual(["192.0.2.0/24", "198.51.100.0/24"]);
    expect(a.subnets[0].ports).toEqual([{ port: "Eth1/1", macs: ["m1", "m2"], shared: true }]);
    expect(a.subnets[1].ports[0]).toEqual({ port: "Eth1/1", macs: ["m4"], shared: true });
    expect(a.subnets[1].ports[1]).toEqual({ port: "Eth1/3", macs: ["m3"], shared: false });
  });
});
