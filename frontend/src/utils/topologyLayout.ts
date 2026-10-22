/**
 * IP 拓樸圖「以交換器為中心」的版面計算（2026-10-21 依分析者建議與參考設計改版）。純函式，vitest 直接測。
 *
 * - 主機只歸一個接入群組：同一個 MAC 在兩台交換器的 FDB 都學得到，多半只是一邊是存取埠、
 *   一邊是上聯埠，不能直接當成主機雙接。直連的埠（那個埠只有這一個 MAC）優先，
 *   其次是埠上 MAC 比較少的那一邊；其餘的連線標成待確認
 * - 名稱放在圖示下方、最多兩行；同名的補上 IP 才分得出來
 * - VPN 從整張圖的外側繞（給 cytoscape 的 segments 折點）
 */
import { breakPoints } from "./warroom";

export interface L2Link {
  id: string;
  source: string;            // 主機
  target: string;            // 交換器
  port?: string | null;
  direct?: boolean | null;
  port_mac_count?: number | null;
}

export interface Owner {
  switch: string;
  edge: string;
  port: string | null;
  direct: boolean;
  /** 同一台主機在別台交換器上被學到的連線（待確認） */
  others: string[];
}

export function assignOwners(links: L2Link[], switchOrder: string[]): Map<string, Owner> {
  const rank = new Map(switchOrder.map((s, i) => [s, i]));
  const byHost = new Map<string, L2Link[]>();
  for (const l of links) (byHost.get(l.source) ?? byHost.set(l.source, []).get(l.source)!).push(l);
  const out = new Map<string, Owner>();
  for (const [host, ls] of byHost) {
    const best = [...ls].sort((a, b) =>
      Number(!!b.direct) - Number(!!a.direct)
      || (a.port_mac_count ?? 9999) - (b.port_mac_count ?? 9999)
      || (rank.get(a.target) ?? 9999) - (rank.get(b.target) ?? 9999))[0];
    out.set(host, {
      switch: best.target, edge: best.id, port: best.port ?? null, direct: !!best.direct,
      others: ls.filter((l) => l !== best).map((l) => l.id),
    });
  }
  return out;
}

/** 一行最多幾個字（圖示下方約 120px 寬） */
const LINE = 22;

function twoLines(name: string): string {
  if (name.length <= LINE) return name;
  const parts = breakPoints(name);
  let first = "";
  let i = 0;
  while (i < parts.length && (first + parts[i]).length <= LINE) first += parts[i++];
  if (!first) {                                // 沒有可以斷的地方：硬切
    first = name.slice(0, LINE);
  }
  let rest = name.slice(first.length);
  if (rest.length > LINE) rest = rest.slice(0, LINE - 1) + "…";
  return `${first}\n${rest}`;
}

export function nodeLabels(nodes: { id: string; label?: string | null; ip?: string | null }[],
                           dupTag?: (i: number, n: number) => string): Map<string, string> {
  const count = new Map<string, number>();
  for (const n of nodes) {
    const k = (n.label ?? "").toLowerCase();
    count.set(k, (count.get(k) ?? 0) + 1);
  }
  const out = new Map<string, string>();
  const seenDup = new Map<string, number>();
  for (const n of nodes) {
    const name = n.label ?? n.id;
    const k = name.toLowerCase();
    const total = count.get(k) ?? 0;
    if (total > 1) {
      // 同名：補 IP；沒有 IP 就標「同名 i/n」—— 至少看得出這是兩個不同的節點、要核對
      const i = (seenDup.get(k) ?? 0) + 1;
      seenDup.set(k, i);
      const one = name.length > LINE ? name.slice(0, LINE - 1) + "…" : name;
      out.set(n.id, `${one}\n${n.ip || (dupTag ? dupTag(i, total) : `${i}/${total}`)}`);
    } else {
      out.set(n.id, twoLines(name));
    }
  }
  return out;
}

/**
 * 折點（絕對座標）→ cytoscape `segment-weights`／`segment-distances`（edge-distances: node-position）。
 * weight＝沿 source→target 的比例，distance＝往右手邊法線方向的距離。
 */
export function segmentsFor(s: { x: number; y: number }, t: { x: number; y: number },
                            points: { x: number; y: number }[]): { weights: number[]; distances: number[] } {
  const vx = t.x - s.x, vy = t.y - s.y;
  const L2 = vx * vx + vy * vy || 1;
  const L = Math.sqrt(L2);
  const nx = -vy / L, ny = vx / L;
  const weights: number[] = [];
  const distances: number[] = [];
  for (const p of points) {
    const px = p.x - s.x, py = p.y - s.y;
    weights.push((px * vx + py * vy) / L2);
    distances.push(px * nx + py * ny);
  }
  return { weights, distances };
}

/* ───────────── 依子網路排版（10-22） ─────────────
 * 使用者：「不要一開始畫那麼開，同一個子網路的要靠近點，不同子網路的才要拉開」。
 * 以前只把子網路節點左右推開、成員留在力導向散開的位置；混合模式的子網路是框（複合節點），
 * 推框不會帶著成員走，框就互相重疊。改成：每個網段自己收成一團（一圈或格子），再一團一團排開。 */

type Pt = { x: number; y: number };

/** n 個點繞著 (0,0) 排成一圈或數圈，相鄰點距離至少 spacing。 */
export function ringPositions(n: number, spacing = 96): Pt[] {
  const out: Pt[] = [];
  let ring = 0;
  while (out.length < n) {
    const r = 120 + ring * spacing;
    const cap = Math.max(1, Math.floor((2 * Math.PI * r) / spacing));
    const k = Math.min(cap, n - out.length);
    const offset = ring % 2 ? Math.PI / k : 0;               // 內外圈錯開，線才不會疊在一起
    for (let i = 0; i < k; i++) {
      const a = -Math.PI / 2 + offset + (2 * Math.PI * i) / k;
      out.push({ x: r * Math.cos(a), y: r * Math.sin(a) });
    }
    ring++;
  }
  return out;
}

/** n 個點排成接近方形（略寬）的格子，以 (0,0) 為中心。 */
export function gridPositions(n: number, dx: number, dy: number): Pt[] {
  if (n <= 0) return [];
  const cols = Math.max(1, Math.ceil(Math.sqrt(n * 1.6)));
  const rows = Math.ceil(n / cols);
  const out: Pt[] = [];
  for (let i = 0; i < n; i++) {
    const r = Math.floor(i / cols), c = i % cols;
    const inRow = r === rows - 1 ? n - r * cols : cols;      // 最後一列置中
    out.push({ x: (c - (inRow - 1) / 2) * dx, y: (r - (rows - 1) / 2) * dy });
  }
  return out;
}

/** 由左到右排，超過 maxWidth 換列；回傳每一團左上角的位置。團與團之間至少隔 gap。 */
export function packRows(items: { id: string; w: number; h: number }[], gap: number, maxWidth: number): Map<string, Pt> {
  const out = new Map<string, Pt>();
  let x = 0, y = 0, rowH = 0;
  for (const it of items) {
    if (x > 0 && x + it.w > maxWidth) { x = 0; y += rowH + gap; rowH = 0; }
    out.set(it.id, { x, y });
    x += it.w + gap;
    rowH = Math.max(rowH, it.h);
  }
  return out;
}
