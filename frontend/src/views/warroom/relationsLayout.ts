/**
 * 09 IP 物件關係的版面（純函式，vitest 直接測）。
 *
 * 三列（實體歸屬、虛擬化對應、位址歸屬）× 三欄，焦點 IP 是右邊一個跨三列的高方塊：每列最後一個物件
 * 直接以水平線接到它（一個 IP 節點、多條有名稱的邊）。後端給每個節點 row／col／slot，這裡只換算座標與畫線：
 * - 同一格兩個物件（來源不一致時）上下疊放；那一列的單一物件改靠上，讓疊放的第二個有一條空的下方走道
 * - 同列往右：水平線；起點與終點高度不同時，在終點前的間隙轉折（跨欄的線走下方走道，不穿過中間的方塊）
 * - 跨列（實體列的主機 → 虛擬化列的 VM）：從方塊下緣往下，在兩列之間的空隙水平移過去，再往下接到上緣
 * 標籤放在最長的那段水平線中間（上面是關係種類，下面是來源與時間）；跨列的標籤放在垂直線右邊。
 */

export type RowKind = "physical" | "virtual" | "address";
export const ROW_KINDS: RowKind[] = ["physical", "virtual", "address"];

export interface RNode {
  key: string; type: string; id: string | null; label: string; sub: string | null;
  row: RowKind | null; col: number; slot: number;
  dtype?: string; platform?: string; host?: boolean; mac?: string | null;
}
export interface REdge {
  from: string; to: string; kind: string; source: string; at: string | null;
  line: "solid" | "dashed"; state: "ok" | "pending"; reason: string | null;
}
export interface Rect { x: number; y: number; w: number; h: number }
export interface PlacedNode extends RNode { rect: Rect; compact: boolean }
export interface RoutedEdge extends REdge {
  d: string;
  label: { x: number; y: number; w: number; align: "center" | "side" };
}

export const CANVAS_W = 1174;
export const ROW_TITLE = 40;
export const BODY = 172;
export const ROW_GAP = 16;
export const NODE_W = 184;
export const GAP = 96;
export const IP_GAP = 140;
export const COL_X = [0, NODE_W + GAP, 2 * (NODE_W + GAP)];
export const IP_X = COL_X[2] + NODE_W + IP_GAP;
export const IP_W = CANVAS_W - IP_X;
const SLOT_H = 80;
/** 標籤比間隙窄這麼多：轉折的垂直線走在間隙邊上，不穿過標籤 */
const LABEL_PAD = 24;
const SLOT_STEP = 92;
const SINGLE_H = 132;

export function rowTop(i: number): number {
  return i * (ROW_TITLE + BODY + ROW_GAP) + ROW_TITLE;
}
export const IP_RECT: Rect = { x: IP_X, y: rowTop(0), w: IP_W, h: rowTop(2) + BODY - rowTop(0) };
export const CANVAS_H = rowTop(2) + BODY;

export function stackedRows(nodes: RNode[]): Set<RowKind> {
  return new Set(nodes.filter((n) => n.row && n.slot > 0).map((n) => n.row as RowKind));
}

export function place(nodes: RNode[]): PlacedNode[] {
  const stacked = stackedRows(nodes);
  return nodes.map((n) => {
    if (!n.row) return { ...n, rect: { ...IP_RECT }, compact: false };
    const top = rowTop(ROW_KINDS.indexOf(n.row));
    const s = stacked.has(n.row);
    const rect = s
      ? { x: COL_X[n.col] ?? 0, y: top + n.slot * SLOT_STEP, w: NODE_W, h: SLOT_H }
      : { x: COL_X[n.col] ?? 0, y: top + (BODY - SINGLE_H) / 2, w: NODE_W, h: SINGLE_H };
    return { ...n, rect, compact: s };
  });
}

const cy = (r: Rect) => r.y + r.h / 2;
const cx = (r: Rect) => r.x + r.w / 2;
const right = (r: Rect) => r.x + r.w;

export function route(e: REdge, byKey: Map<string, PlacedNode>): RoutedEdge | null {
  const s = byKey.get(e.from);
  const t = byKey.get(e.to);
  if (!s || !t) return null;
  const S = s.rect, T = t.rect;
  if (!t.row) {
    // 接到焦點 IP：從來源方塊右側水平接到 IP 方塊左緣（高度跟著來源）
    const y = cy(S);
    return { ...e, d: `M ${right(S)} ${y} H ${T.x}`,
      label: { x: (right(S) + T.x) / 2, y, w: T.x - right(S) - LABEL_PAD, align: "center" } };
  }
  if (s.row === t.row && T.x > right(S)) {
    const y0 = cy(S), y1 = cy(T);
    if (y0 === y1) {
      return { ...e, d: `M ${right(S)} ${y0} H ${T.x}`,
        label: { x: (right(S) + T.x) / 2, y: y0, w: T.x - right(S) - LABEL_PAD, align: "center" } };
    }
    // 在終點前的間隙最右邊轉折：間隙中間是那一格自己那條邊的標籤（標籤左右各留 LABEL_PAD / 2）
    const ex = T.x - 6;
    return { ...e, d: `M ${right(S)} ${y0} H ${ex} V ${y1} H ${T.x}`,
      label: { x: (right(S) + T.x - GAP) / 2, y: y0, w: T.x - GAP - right(S) - LABEL_PAD, align: "center" } };
  }
  // 跨列：下緣 → 兩列之間的空隙 → 目標上緣
  const down = T.y > S.y;
  const sx = cx(S), tx = cx(T);
  const midY = down ? T.y - ROW_TITLE - ROW_GAP / 2 : S.y - ROW_TITLE - ROW_GAP / 2;
  const d = down
    ? `M ${sx} ${S.y + S.h} V ${midY} H ${tx} V ${T.y}`
    : `M ${sx} ${S.y} V ${midY} H ${tx} V ${T.y + T.h}`;
  return { ...e, d, label: { x: tx + 10, y: down ? T.y - ROW_TITLE / 2 : T.y + T.h + ROW_TITLE / 2, w: 220, align: "side" } };
}

export function layout(nodes: RNode[], edges: REdge[]): { nodes: PlacedNode[]; edges: RoutedEdge[] } {
  const placed = place(nodes);
  const byKey = new Map(placed.map((n) => [n.key, n]));
  const routed = edges.map((e) => route(e, byKey)).filter((e): e is RoutedEdge => e !== null);
  // 虛線先畫、實線後畫；待確認最後畫（顏色要看得到）
  const rank = (e: REdge) => (e.state === "pending" ? 2 : e.line === "dashed" ? 0 : 1);
  routed.sort((a, b) => rank(a) - rank(b));
  return { nodes: placed, edges: routed };
}

/** 位址的換行點：句點、冒號、斜線之後（不在數字中間斷開；不加省略號） */
export function addrParts(s: string): string[] {
  return s.split(/(?<=[.:/])/);
}

/** 邊與來源的時間：同一天只寫時:分，否則寫月-日（電視上要短） */
export function shortTime(iso: string | null | undefined, now: Date): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 右欄用的時間：同一天時:分，否則月-日 時:分 */
export function longTime(iso: string | null | undefined, now: Date): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? hm : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
}
