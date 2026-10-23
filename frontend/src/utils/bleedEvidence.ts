/**
 * 「子網段混在同一個二層」的例子整理（使用者 10-22：「這感覺還是很亂」）。
 *
 * - ARP：A 用 B 的 MAC 回應、B 也用 A 的 MAC 回應 —— 這是同一台主機兩張網卡（ARP flux）的典型樣子，
 *   合併成一列「A ⇄ B」；只有單向的照列「A → MAC（屬於 B）」。
 * - MAC 表：同一台交換器、同一個 VLAN 合併成一塊（後端可能分成好幾筆），每個子網段一行「埠 ×MAC 數」；
 *   兩個子網段都出現在同一個埠上的標出來（上聯埠或混接點，看的重點）。
 */
export interface ArpExample { ip: string; mac: string; subnet?: string | null; mac_owner_ip?: string | null; mac_owner_subnet?: string | null }
export interface FdbExample { switch: string; vlan: string | number | null; macs?: Record<string, { port?: string | null; mac: string }[]> }

export type ArpLine =
  | { kind: "pair"; a: string; b: string; macA: string; macB: string }
  | { kind: "one"; ip: string; mac: string; owner: string | null };

export function arpLines(list: ArpExample[]): ArpLine[] {
  const seen = new Set<string>();
  const rows = list.filter((x) => { const k = `${x.ip}|${x.mac}`; return seen.has(k) ? false : (seen.add(k), true); });
  const used = new Set<number>();
  const out: ArpLine[] = [];
  rows.forEach((x, i) => {
    if (used.has(i)) return;
    const j = rows.findIndex((y, n) => n !== i && !used.has(n) && y.ip === x.mac_owner_ip && y.mac_owner_ip === x.ip);
    if (j >= 0) {
      used.add(i); used.add(j);
      // x.ip 回應了 x.mac（屬於 x.mac_owner_ip）；macA＝A 這個位址真正的 MAC＝對方回應時用的那個
      out.push({ kind: "pair", a: x.ip, b: rows[j].ip, macA: rows[j].mac, macB: x.mac });
    } else {
      used.add(i);
      out.push({ kind: "one", ip: x.ip, mac: x.mac, owner: x.mac_owner_ip ?? null });
    }
  });
  return out;
}

export interface FdbPort { port: string; macs: string[]; shared: boolean }
export interface FdbBlock { key: string; switch: string; vlan: string; subnets: { cidr: string; ports: FdbPort[] }[] }

export function fdbBlocks(list: FdbExample[]): FdbBlock[] {
  const blocks = new Map<string, { sw: string; vlan: string; by: Map<string, Map<string, Set<string>>> }>();
  for (const x of list) {
    const vlan = x.vlan == null ? "" : String(x.vlan);
    const key = `${x.switch}|${vlan}`;
    let b = blocks.get(key);
    if (!b) { b = { sw: x.switch, vlan, by: new Map() }; blocks.set(key, b); }
    for (const [cidr, macs] of Object.entries(x.macs ?? {})) {
      const ports = b.by.get(cidr) ?? new Map<string, Set<string>>();
      b.by.set(cidr, ports);
      for (const m of macs) {
        const p = m.port || "—";
        ports.set(p, (ports.get(p) ?? new Set()).add(m.mac));
      }
    }
  }
  return [...blocks.entries()].map(([key, b]) => {
    const cidrsOfPort = new Map<string, number>();
    for (const ports of b.by.values()) for (const p of ports.keys()) cidrsOfPort.set(p, (cidrsOfPort.get(p) ?? 0) + 1);
    return {
      key, switch: b.sw, vlan: b.vlan,
      subnets: [...b.by.entries()].map(([cidr, ports]) => ({
        cidr,
        ports: [...ports.entries()]
          .map(([port, macs]) => ({ port, macs: [...macs], shared: (cidrsOfPort.get(port) ?? 0) > 1 }))
          .sort((p, q) => Number(q.shared) - Number(p.shared) || q.macs.length - p.macs.length),
      })),
    };
  });
}
