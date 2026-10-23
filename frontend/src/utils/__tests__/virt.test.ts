import { describe, expect, it } from "vitest";
import { virtRouteFor, virtTagText, virtTech, vmProject } from "../virt";

describe("virt 平台對照", () => {
  it("每個平台都有自己的虛擬化頁", () => {
    expect(virtRouteFor("proxmox")).toBe("virt");
    expect(virtRouteFor("vmware")).toBe("virt_vmware");
    expect(virtRouteFor("hyper-v")).toBe("virt_hyperv");
    expect(virtRouteFor("openstack")).toBe("virt_openstack");
    expect(virtRouteFor(null)).toBe("virt");
  });
  it("技術名稱", () => {
    expect(virtTech("proxmox", "ct")).toBe("LXC");
    expect(virtTech("proxmox", "vm")).toBe("KVM");
    expect(virtTech("proxmox", null)).toBe("");
    expect(virtTech("openstack", "vm")).toBe("OpenStack");
    expect(virtTagText({ platform: "openstack", kind: "vm" }, (k) => (k.endsWith("vm_tag") ? "VM" : "CT")))
      .toBe("VM · OpenStack");
  });
});

describe("vmProject", () => {
  it("有 project 欄位優先", () => {
    expect(vmProject({ project: "demo", description: "project: other" })).toBe("demo");
  });
  it("從說明取出", () => {
    expect(vmProject({ description: "project: 研發 A" })).toBe("研發 A");
    expect(vmProject({ description: "something\nProject:  prod " })).toBe("prod");
  });
  it("沒有就回 null", () => {
    expect(vmProject({})).toBeNull();
    expect(vmProject({ description: "just text" })).toBeNull();
    expect(vmProject({ description: "project:   " })).toBeNull();
  });
});
