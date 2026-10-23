/**
 * 「虛實」的標籤：虛擬化整合對應到的是哪一種客體（使用者要求，2026-10-02：PVE 有 KVM／LXC 兩種）。
 *
 * PVE：kind="vm"（qemu）＝KVM 虛擬機、kind="ct"（lxc）＝LXC 容器；VMware 的都是虛擬機。
 * 平台或種類不明時只回大類，不猜。
 */
export interface VirtMatch { vm?: string; cluster?: string | null; platform?: string | null; kind?: string | null }

/** 平台（virt_clusters.type）＋客體種類 → 技術名稱；不明時回空字串。平台清單只寫在這裡 */
export function virtTech(platform: string | null | undefined, kind: string | null | undefined): string {
  if (platform === "proxmox") return kind === "ct" ? "LXC" : kind === "vm" ? "KVM" : "";
  if (platform === "vmware") return "VMware";
  if (platform === "hyper-v") return "Hyper-V";
  if (platform === "openstack") return "OpenStack";
  return "";
}

export function virtTagText(v: VirtMatch, t: (k: string) => string): string {
  const base = v.kind === "ct" ? t("addresses.virt_ct_tag") : t("addresses.virt_vm_tag");
  const tech = virtTech(v.platform, v.kind);
  return tech ? `${base} · ${tech}` : base;
}

/** 虛擬化平台（virt_clusters.type）→ 那個平台的虛擬化頁路由名稱。平台清單只寫在這裡，各處共用 */
export function virtRouteFor(platform: string | null | undefined): string {
  if (platform === "vmware") return "virt_vmware";
  if (platform === "hyper-v" || platform === "hyperv") return "virt_hyperv";
  if (platform === "openstack") return "virt_openstack";
  return "virt";
}

/**
 * OpenStack VM 所屬的專案（project）。
 * 後端有 project 欄位就用；沒有時從同步寫進的說明「project: 名稱」取出。都沒有回 null。
 */
export function vmProject(vm: { project?: string | null; description?: string | null }): string | null {
  if (vm.project) return vm.project;
  const m = /^project:[ \t]*(.*)$/im.exec(vm.description ?? "");
  return m ? m[1].trim() || null : null;
}
