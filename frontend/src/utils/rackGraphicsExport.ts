// 機櫃／層架示意圖的圖形匯出（SVG / PNG / draw.io）。
//
// 同一套幾何供「單一機櫃」與「合併單卡（整個機房並排）」共用：傳入 diagrams 陣列即可，
// 多櫃會像畫面一樣並排、**底部對齊**（矮的往下推）。
//
// ⚠️ 這支以前是「每列固定 24px、一台裝置佔滿整列」的模型，與畫面早就對不上了：
//   - 對齊基準 `alignTo` 在畫面上已改成**像素**（層架的列高是機櫃的好幾倍，用 U 數算會
//     把層架推出畫面），匯出這邊還當成 U 數去乘列高 → 圖高變成數萬 px，整張幾乎全空白。
//   - 橫向位置還在讀早就被 rack_slot/rack_slot_span 取代的 `rack_side`，層內上下疊放
//     （rack_vslot）更是完全沒有 → 並排的兩台互相蓋住、疊放的看不出來。
//   - 層架被當成機櫃畫：沒有層板、每層一樣高、放在頂板上面的那台整個不見。
// 現在幾何直接吃後端給的 render_* 像素值，與畫面同一套。
import { rackTypeColor as colorFor } from "@/utils/rackColors";

export type RackNameAlign = "left" | "center" | "right";

/** 匯出需要的機櫃資料 —— 就是 `/racks/{id}/diagram` 回的那份。 */
export interface ExportDiagram {
  name: string;
  u_height: number;
  devices: any[];
  kind?: string | null;
  numbering?: string | null;
  open_top?: boolean;
  render_row_px?: number;
  render_row_px_list?: number[];
  render_board_px?: number;
  render_width_px?: number;
  /** 「20U」或「9 層」；文案在元件那一端翻譯，這裡只負責畫。 */
  rowsLabel?: string;
  /** 開放頂那一列的標籤（預設「頂」）。 */
  topLabel?: string;
}

export interface RowBox {
  /** 層號／U 號；開放頂那一列是 u_height + 1 */
  u: number;
  isTop: boolean;
  /** 距離機櫃頂端多少 px */
  y: number;
  /** 這一列的高度，**含**下緣那片層板 */
  h: number;
}

const SLOTS = 60;                     // 與後端 rack.py 的 RACK_SLOTS 一致
const GEO = { gutter: 32, pad: 12, headerH: 30 };
const COL_GAP = 40;                   // 機櫃之間的水平間距
const SHELF_KINDS = new Set(["shelf", "wire_shelf", "wood_shelf"]);

function esc(s: string): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function boardPx(d: ExportDiagram): number {
  return Number(d.render_board_px ?? 0) || 0;
}

function colWidth(d: ExportDiagram): number {
  return Math.max(140, Number(d.render_width_px ?? 260) || 260);
}

/**
 * 由上而下的每一列（含「頂板上方」那一列）與它在圖上的 y。
 *
 * 與畫面的 cells 同一套規則：層高逐層可調、開放頂多一列且沿用最上層的高度、
 * bottom-up 時第 1 層畫在最上面，但「頂」永遠是實體最上面那一列。
 */
export function rowBoxes(d: ExportDiagram): RowBox[] {
  const real = Math.max(0, Number(d.u_height ?? 0));
  if (!real) return [];
  const openTop = Boolean(d.open_top);
  const n = real + (openTop ? 1 : 0);
  const base = Number(d.render_row_px ?? 28) || 28;
  const list = (d.render_row_px_list ?? []).slice(0, real).map((v) => Number(v) || base);
  while (list.length < real) list.push(base);
  const pxOf = (u: number) => list[u - 1] ?? list[list.length - 1] ?? base;
  const board = boardPx(d);

  const bottomUp = d.numbering === "bottom-up";
  const order = bottomUp
    ? Array.from({ length: n }, (_, i) => i + 1)
    : Array.from({ length: n }, (_, i) => n - i);
  if (openTop && bottomUp) {
    const i = order.indexOf(n);
    if (i > 0) order.unshift(...order.splice(i, 1));
  }
  const out: RowBox[] = [];
  let y = 0;
  for (const u of order) {
    const h = pxOf(u) + board;
    out.push({ u, isTop: u > real, y, h });
    y += h;
  }
  return out;
}

/**
 * 側架／立柱的範圍。開放頂的層架**立柱只到最上面那片層板為止** —— 板子上面是開放的，
 * 東西就放在那裡，柱子不會再往上長（IVAR 實物就是這樣，畫面也是這樣畫的）。
 */
export function postSpan(d: ExportDiagram): { y: number; h: number } {
  const rows = rowBoxes(d);
  const total = rows.length ? rows[rows.length - 1].y + rows[rows.length - 1].h : 0;
  if (!rows.length || !rows[0].isTop) return { y: 0, h: total };
  // 第一列是「頂板上方」：柱子從那片板的**頂端**起算（板厚不扣，板本身是柱子撐著的）
  const y = Math.max(0, rows[0].h - boardPx(d));
  return { y, h: total - y };
}

/** 一台機櫃畫出來多高（px）。並排對齊要用這個，不是 U 數。 */
export function rackHeightPx(d: ExportDiagram): number {
  const rows = rowBoxes(d);
  return rows.length ? rows[rows.length - 1].y + rows[rows.length - 1].h : 0;
}

/**
 * 一台裝置在機櫃內的方塊（x 以機櫃左緣為 0）。
 *
 * 橫向是「起始格 + 跨幾格」的 60 格網格；層內上下同一套，而且**由下往上**長
 * （裝置站在層板上）。放不進任何一列就回 null —— 寧可不畫，也不要畫到奇怪的地方。
 */
export function deviceBox(
  d: ExportDiagram, dev: any, colW: number,
): { x: number; y: number; w: number; h: number } | null {
  const pos = Number(dev?.u_position);
  const size = Number(dev?.u_size);
  if (!Number.isFinite(pos) || !Number.isFinite(size) || pos <= 0 || size <= 0) return null;
  const rows = rowBoxes(d);
  const mine = rows.filter((r) => r.u >= pos && r.u < pos + size);
  if (!mine.length) return null;
  const yTop = Math.min(...mine.map((r) => r.y));
  const total = mine.reduce((a, r) => a + r.h, 0);
  const content = Math.max(1, total - boardPx(d));   // 最下面那片板不是可放空間

  const s0 = Number(dev.rack_slot ?? 0) || 0;
  const ss = Number(dev.rack_slot_span ?? SLOTS) || SLOTS;
  const v0 = Number(dev.rack_vslot ?? 0) || 0;
  const vs = Number(dev.rack_vslot_span ?? SLOTS) || SLOTS;
  return {
    x: (s0 / SLOTS) * colW,
    w: (ss / SLOTS) * colW,
    h: (vs / SLOTS) * content,
    y: yTop + content - ((v0 + vs) / SLOTS) * content,
  };
}

/** 並排版面：每台機櫃的左緣與頂端 y（底部對齊）。 */
function layout(diagrams: ExportDiagram[], alignToPx: number) {
  const { gutter, pad, headerH } = GEO;
  const heights = diagrams.map(rackHeightPx);
  const maxH = Math.max(alignToPx || 0, ...heights, 1);
  let x = pad;
  const blocks = diagrams.map((d, i) => {
    const colW = colWidth(d);
    const rackLeft = x + gutter;
    x += gutter + colW + COL_GAP;
    return { d, rackLeft, colW, top: headerH + pad + (maxH - heights[i]), h: heights[i] };
  });
  return { blocks, W: x - COL_GAP + pad, H: headerH + pad * 2 + maxH };
}

/** 層架的側架寬度（畫面上是 14px 的松木／鍍鉻立柱；機櫃是導軌）。 */
function sideWidth(kind: string | null | undefined): number {
  return SHELF_KINDS.has(kind || "") ? 12 : 8;
}

function boardColor(kind: string | null | undefined): string {
  if (kind === "wood_shelf") return "#b5813f";
  if (kind === "wire_shelf") return "#b9bfc5";
  return "#9aa0a6";
}

function postColor(kind: string | null | undefined): string {
  if (kind === "wood_shelf") return "#d4ae7f";
  if (kind === "wire_shelf") return "#c8ced4";
  return "#c3c8ce";
}

export function buildRacksSvg(
  diagrams: ExportDiagram[], alignToPx: number, nameAlign: RackNameAlign,
): { svg: string; W: number; H: number } | null {
  if (!diagrams.length) return null;
  const { pad } = GEO;
  const { blocks, W, H } = layout(diagrams, alignToPx);
  const p: string[] = [];
  p.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" font-family="sans-serif">`);
  p.push(`<rect x="0" y="0" width="${W}" height="${H}" fill="#ffffff"/>`);
  for (const { d, rackLeft, colW, top, h } of blocks) {
    const kind = d.kind ?? "rack";
    const shelf = SHELF_KINDS.has(kind);
    const rows = rowBoxes(d);
    const board = boardPx(d);
    const sw = sideWidth(kind);
    const label = d.rowsLabel ?? `${d.u_height}U`;
    p.push(`<text x="${rackLeft}" y="${pad + 16}" font-size="14" font-weight="bold">${esc(d.name)} (${esc(label)})</text>`);

    // 機櫃是箱體、層架只有兩支側架 + 層板
    if (!shelf) {
      p.push(`<rect x="${rackLeft}" y="${top}" width="${colW}" height="${h}" fill="#f5f5f5" stroke="#888" stroke-width="1.5"/>`);
    }
    const post = postSpan(d);
    for (const px of [rackLeft - sw, rackLeft + colW]) {
      p.push(`<rect x="${px}" y="${top + post.y}" width="${sw}" height="${post.h}" fill="${postColor(kind)}" stroke="#8a8f95" stroke-width="0.5"/>`);
    }

    for (const r of rows) {
      const y = top + r.y;
      const num = r.isTop ? (d.topLabel ?? "頂") : String(r.u);
      p.push(`<text x="${rackLeft - sw - 4}" y="${y + r.h / 2 + 4}" font-size="10" text-anchor="end" fill="#666">${esc(num)}</text>`);
      if (shelf && board > 0) {
        // 層板畫在每一列的下緣（頂板上方那一列的下緣就是頂板）
        p.push(`<rect x="${rackLeft}" y="${y + r.h - board}" width="${colW}" height="${board}" fill="${boardColor(kind)}"/>`);
      } else if (!shelf) {
        p.push(`<line x1="${rackLeft}" y1="${y}" x2="${rackLeft + colW}" y2="${y}" stroke="#dddddd" stroke-width="0.5"/>`);
      }
    }

    for (const dev of (d.devices || [])) {
      const b = deviceBox(d, dev, colW);
      if (!b) continue;
      const x = rackLeft + b.x;
      const y = top + b.y;
      p.push(`<rect x="${x + 1}" y="${y + 1}" width="${Math.max(1, b.w - 2)}" height="${Math.max(1, b.h - 2)}" fill="${colorFor(dev.type)}" stroke="rgba(0,0,0,0.3)"/>`);
      const narrow = b.w < colW - 1;
      const a = nameAlign;
      const tx = narrow ? x + b.w / 2
        : a === "center" ? x + b.w / 2 : a === "right" ? x + b.w - 10 : x + 10;
      const anchor = narrow ? "middle" : a === "center" ? "middle" : a === "right" ? "end" : "start";
      p.push(`<text x="${tx}" y="${y + b.h / 2 + 4}" text-anchor="${anchor}" font-size="11" font-weight="bold" fill="#ffffff">${esc(dev.name ?? "")}</text>`);
      if (dev.rack_face === "rear") {
        const rx = x + b.w;
        p.push(`<path d="M${rx - 14} ${y + 1} L${rx} ${y + 1} L${rx} ${y + 15} Z" fill="rgba(0,0,0,0.55)"/>`);
        p.push(`<text x="${rx - 2}" y="${y + 11}" text-anchor="end" font-size="9" font-weight="bold" fill="#ffffff">R</text>`);
      }
    }
  }
  p.push(`</svg>`);
  return { svg: p.join("\n"), W, H };
}

export function buildRacksDrawio(
  diagrams: ExportDiagram[], alignToPx: number, nameAlign: RackNameAlign, title: string,
): string | null {
  if (!diagrams.length) return null;
  const { pad } = GEO;
  const { blocks } = layout(diagrams, alignToPx);
  const cells: string[] = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>'];
  let n = 0;
  for (const { d, rackLeft, colW, top, h } of blocks) {
    const kind = d.kind ?? "rack";
    const shelf = SHELF_KINDS.has(kind);
    const rows = rowBoxes(d);
    const board = boardPx(d);
    const sw = sideWidth(kind);
    const label = d.rowsLabel ?? `${d.u_height}U`;
    cells.push(`<mxCell id="t${n++}" value="${esc(`${d.name} (${label})`)}" style="text;html=1;align=left;verticalAlign=middle;fontStyle=1;fontSize=14;" vertex="1" parent="1"><mxGeometry x="${rackLeft}" y="${pad}" width="${colW}" height="20" as="geometry"/></mxCell>`);
    if (!shelf) {
      cells.push(`<mxCell id="r${n++}" value="" style="rounded=0;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#888888;strokeWidth=2;" vertex="1" parent="1"><mxGeometry x="${rackLeft}" y="${top}" width="${colW}" height="${h}" as="geometry"/></mxCell>`);
    }
    const post = postSpan(d);
    for (const side of [rackLeft - sw, rackLeft + colW]) {
      cells.push(`<mxCell id="p${n++}" value="" style="rounded=0;whiteSpace=wrap;html=1;fillColor=${postColor(kind)};strokeColor=#8a8f95;" vertex="1" parent="1"><mxGeometry x="${side}" y="${top + post.y}" width="${sw}" height="${post.h}" as="geometry"/></mxCell>`);
    }
    for (const r of rows) {
      const y = top + r.y;
      const num = r.isTop ? (d.topLabel ?? "頂") : String(r.u);
      cells.push(`<mxCell id="u${n++}" value="${esc(num)}" style="text;html=1;align=right;verticalAlign=middle;fontSize=10;fontColor=#666666;" vertex="1" parent="1"><mxGeometry x="${rackLeft - sw - 28}" y="${y}" width="24" height="${r.h}" as="geometry"/></mxCell>`);
      if (shelf && board > 0) {
        cells.push(`<mxCell id="b${n++}" value="" style="rounded=0;whiteSpace=wrap;html=1;fillColor=${boardColor(kind)};strokeColor=none;" vertex="1" parent="1"><mxGeometry x="${rackLeft}" y="${y + r.h - board}" width="${colW}" height="${board}" as="geometry"/></mxCell>`);
      }
    }
    for (const dev of (d.devices || [])) {
      const b = deviceBox(d, dev, colW);
      if (!b) continue;
      const align = b.w < colW - 1 ? "center" : nameAlign;
      cells.push(`<mxCell id="dev${n++}" value="${esc(dev.name ?? "")}" style="rounded=0;whiteSpace=wrap;html=1;fillColor=${colorFor(dev.type)};strokeColor=#000000;fontColor=#ffffff;fontStyle=1;align=${align};spacingLeft=6;spacingRight=6;" vertex="1" parent="1"><mxGeometry x="${rackLeft + b.x + 1}" y="${top + b.y + 1}" width="${Math.max(1, b.w - 2)}" height="${Math.max(1, b.h - 2)}" as="geometry"/></mxCell>`);
    }
  }
  return (
    `<mxfile host="jt-ipam"><diagram name="${esc(title)}">` +
    `<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" math="0" shadow="0">` +
    `<root>${cells.join("")}</root></mxGraphModel></diagram></mxfile>`
  );
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

export function exportRacksSvg(
  diagrams: ExportDiagram[], alignToPx: number, nameAlign: RackNameAlign, filename: string,
): void {
  const r = buildRacksSvg(diagrams, alignToPx, nameAlign);
  if (!r) return;
  downloadBlob(new Blob([r.svg], { type: "image/svg+xml" }), `${filename}.svg`);
}

export function exportRacksPng(
  diagrams: ExportDiagram[], alignToPx: number, nameAlign: RackNameAlign, filename: string,
): void {
  const r = buildRacksSvg(diagrams, alignToPx, nameAlign);
  if (!r) return;
  const scale = 2;
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = r.W * scale;
    canvas.height = r.H * scale;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0);
    canvas.toBlob((blob) => { if (blob) downloadBlob(blob, `${filename}.png`); }, "image/png");
  };
  img.src = "data:image/svg+xml;base64," +
    btoa(unescape(encodeURIComponent(r.svg)));
}

export function exportRacksDrawio(
  diagrams: ExportDiagram[], alignToPx: number, nameAlign: RackNameAlign, filename: string,
): void {
  const xml = buildRacksDrawio(diagrams, alignToPx, nameAlign, filename);
  if (!xml) return;
  downloadBlob(new Blob([xml], { type: "application/xml" }), `${filename}.drawio`);
}
