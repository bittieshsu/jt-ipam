/**
 * 另一面的裝置提示（2026-10-21 設計：「側邊提示軌＋背面數量」）。
 *
 * 看正面時要知道背面有東西（反之亦然），但不能遮住這一面的裝置：
 * - 標準機櫃：對齊 U 位，重疊或相鄰的併成一段（跨多 U 用括線），同一台只算一次
 * - 層架：每層一個提示，不跨層合併；跨多層的裝置算在它最底下那一層
 * - 格櫃（KALLAX）：再依橫向位置分到第幾欄，提示放在對應的格位
 * 提示只是「那裡有東西」：背面有裝置不代表正面不能放（看深度與占用規則，後端判斷）。
 */
import { RACK_SLOTS, usesLevels } from "./rackSlots";

export interface FaceDevice {
  device_id: string;
  name: string;
  type: string;
  model?: string | null;
  vendor?: string | null;
  u_position: number;
  u_size: number;
  rack_face?: "front" | "rear" | null;
  rack_slot?: number;
  rack_slot_span?: number;
}

export interface FaceHint {
  /** 這段涵蓋的 U／層（含頭尾）；層架與格櫃 from＝to */
  from: number;
  to: number;
  /** 格櫃才有：第幾欄（0 起算） */
  col: number | null;
  devices: FaceDevice[];
}

const faceOf = (d: FaceDevice) => (d.rack_face ?? "front");

export function otherFaceHints(devices: FaceDevice[], viewing: "front" | "rear", kind: string | null | undefined,
                               cols = 1): FaceHint[] {
  const other = devices.filter((d) => faceOf(d) !== viewing && d.u_position > 0)
    .sort((a, b) => a.u_position - b.u_position || a.name.localeCompare(b.name));
  if (!other.length) return [];
  const byName = (a: FaceDevice, b: FaceDevice) => a.name.localeCompare(b.name);

  if (!usesLevels(kind)) {
    // 區間聯集：重疊或相鄰（上一段的頂端＋1 ≥ 下一台的底）就併
    const runs: FaceHint[] = [];
    for (const d of other) {
      const lo = d.u_position;
      const hi = d.u_position + Math.max(1, d.u_size) - 1;
      const last = runs[runs.length - 1];
      if (last && lo <= last.to + 1) {
        last.to = Math.max(last.to, hi);
        last.devices.push(d);
      } else {
        runs.push({ from: lo, to: hi, col: null, devices: [d] });
      }
    }
    runs.forEach((r) => r.devices.sort(byName));
    return runs.reverse();                       // 畫面由上往下：U 高的先
  }

  const kallax = kind === "kallax" && cols > 1;
  const width = RACK_SLOTS / Math.max(1, cols);
  const groups = new Map<string, FaceHint>();
  for (const d of other) {
    const level = d.u_position;
    let col: number | null = null;
    if (kallax) {
      const mid = Number(d.rack_slot ?? 0) + Number(d.rack_slot_span ?? RACK_SLOTS) / 2;
      col = Math.min(cols - 1, Math.max(0, Math.floor((mid - 0.001) / width)));
    }
    const key = `${level}:${col ?? ""}`;
    const g = groups.get(key) ?? { from: level, to: level, col, devices: [] };
    g.devices.push(d);
    groups.set(key, g);
  }
  const out = [...groups.values()];
  out.forEach((g) => g.devices.sort(byName));
  return out.sort((a, b) => b.from - a.from || (a.col ?? 0) - (b.col ?? 0));
}
