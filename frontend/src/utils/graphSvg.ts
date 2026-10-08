/**
 * 關係圖匯出 SVG（使用者 2026-10-08：匯出也要有 SVG）。不加套件：直接用 cytoscape 算好的座標與樣式畫成向量圖，
 * 節點（圓／卡片＋類型圖示）、名稱、線（粗細、虛線、箭頭、樹狀的轉折）、線上的字、依影響分層的圈與標題都在。
 * 名稱跟畫面一樣會截斷（SVG 不會自己加…），截斷用 canvas 量字寬。
 */
import type cytoscape from "cytoscape";

const FONT = "'Noto Sans TC', 'Noto Sans CJK TC', 'Microsoft JhengHei', 'PingFang TC', system-ui, sans-serif";

export function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

let ctx2d: CanvasRenderingContext2D | null | undefined;
function measure(text: string, size: number, bold: boolean): number {
  if (ctx2d === undefined) {
    // jsdom（單元測試）沒有 canvas：直接用估計值，不要去呼叫（它會印一行 Not implemented）
    const jsdom = typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent);
    try { ctx2d = !jsdom && typeof document !== "undefined" ? document.createElement("canvas").getContext("2d") : null; }
    catch { ctx2d = null; }
  }
  if (!ctx2d) return text.length * size * 0.62;   // 沒有 canvas（測試環境）時的估計
  ctx2d.font = `${bold ? "bold " : ""}${size}px ${FONT}`;
  return ctx2d.measureText(text).width;
}
export function ellipsize(text: string, maxW: number, size: number, bold = false): string {
  if (!text || measure(text, size, bold) <= maxW) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (measure(`${text.slice(0, mid)}…`, size, bold) <= maxW) lo = mid; else hi = mid - 1;
  }
  return `${text.slice(0, lo)}…`;
}

const num = (v: unknown, d = 0) => {
  const n = parseFloat(String(v ?? ""));
  return Number.isFinite(n) ? n : d;
};

export function graphToSvg(cy: cytoscape.Core, opts: { bg: string; fg: string }): string {
  const els = cy.elements();
  const bb = els.boundingBox({ includeLabels: true } as any);
  const pad = 24;
  const x0 = bb.x1 - pad, y0 = bb.y1 - pad, w = Math.max(bb.w, 1) + pad * 2, h = Math.max(bb.h, 1) + pad * 2;
  const out: string[] = [];
  out.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${x0.toFixed(1)} ${y0.toFixed(1)} ${w.toFixed(1)} ${h.toFixed(1)}" `
    + `width="${Math.round(w)}" height="${Math.round(h)}" font-family="${esc(FONT)}">`);
  out.push(`<rect x="${x0.toFixed(1)}" y="${y0.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" fill="${opts.bg}"/>`);

  // 依影響分層的圈（最底層）
  cy.nodes().filter((n) => n.hasClass("zone")).forEach((n) => {
    const p = n.position(), r = num(n.data("size")) / 2, c = String(n.data("color"));
    out.push(`<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="none" stroke="${c}" stroke-opacity="0.55" stroke-width="1.5" stroke-dasharray="5 4"/>`);
    out.push(`<text x="${p.x}" y="${p.y - r - 7}" text-anchor="middle" font-size="13" font-weight="bold" fill="${c}">${esc(String(n.data("label") ?? ""))}</text>`);
  });

  // 線
  cy.edges().forEach((e) => {
    const s = e.source(), t = e.target();
    let a: { x: number; y: number }, b: { x: number; y: number };
    try { a = e.sourceEndpoint(); b = e.targetEndpoint(); } catch { a = s.position(); b = t.position(); }
    if (!Number.isFinite(a?.x) || !Number.isFinite(b?.x)) { a = s.position(); b = t.position(); }
    let mids: { x: number; y: number }[] = [];
    try { mids = (e.segmentPoints?.() as { x: number; y: number }[] | undefined) ?? []; } catch { mids = []; }
    const pts = [a, ...mids, b];
    const color = String(e.style("line-color") || "#c4c8cf");
    const width = num(e.style("width"), 1.2);
    const dashed = e.style("line-style") === "dashed";
    out.push(`<polyline points="${pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")}" fill="none" stroke="${color}" `
      + `stroke-width="${width}"${dashed ? ' stroke-dasharray="6 4"' : ""}/>`);
    // 箭頭：沿最後一段的方向畫一個小三角形
    const p1 = pts[pts.length - 2], p2 = pts[pts.length - 1];
    const ang = Math.atan2(p2.y - p1.y, p2.x - p1.x), L = 7 + width * 1.5, W = 4 + width;
    const bx = p2.x - L * Math.cos(ang), by = p2.y - L * Math.sin(ang);
    const lx = bx + W * Math.sin(ang), ly = by - W * Math.cos(ang), rx = bx - W * Math.sin(ang), ry = by + W * Math.cos(ang);
    out.push(`<polygon points="${p2.x.toFixed(1)},${p2.y.toFixed(1)} ${lx.toFixed(1)},${ly.toFixed(1)} ${rx.toFixed(1)},${ry.toFixed(1)}" fill="${color}"/>`);
    // 線上的字：畫面上有顯示的才畫（連到評估目標的放在靠外側那一端）
    if (e.hasClass("lbl") && e.data("label")) {
      const text = String(e.data("label"));
      let lp: { x: number; y: number };
      if (e.data("toRoot")) {
        const off = Math.min(num(e.style("source-text-offset"), 58), Math.hypot(b.x - a.x, b.y - a.y) / 2);
        const q = pts[1] ?? b;
        const d = Math.hypot(q.x - a.x, q.y - a.y) || 1;
        lp = { x: a.x + ((q.x - a.x) / d) * off, y: a.y + ((q.y - a.y) / d) * off };
      } else {
        lp = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      }
      const tw = measure(text, 11, false);
      out.push(`<rect x="${(lp.x - tw / 2 - 2).toFixed(1)}" y="${(lp.y - 8).toFixed(1)}" width="${(tw + 4).toFixed(1)}" height="15" fill="${opts.bg}" fill-opacity="0.9"/>`);
      out.push(`<text x="${lp.x.toFixed(1)}" y="${(lp.y + 4).toFixed(1)}" text-anchor="middle" font-size="11" fill="${opts.fg}">${esc(text)}</text>`);
    }
  });

  // 節點
  cy.nodes().filter((n) => !n.hasClass("zone")).forEach((n) => {
    const p = n.position(), nw = n.width(), nh = n.height();
    const card = n.hasClass("card");
    const rect = card || n.data("group");
    const fill = String(n.style("background-color") || "#888");
    const stroke = String(n.style("border-color") || "none"), bw = num(n.style("border-width"), 0);
    if (rect) {
      out.push(`<rect x="${(p.x - nw / 2).toFixed(1)}" y="${(p.y - nh / 2).toFixed(1)}" width="${nw.toFixed(1)}" height="${nh.toFixed(1)}" `
        + `rx="${card ? 8 : 6}" fill="${fill}" stroke="${stroke}" stroke-width="${bw}"/>`);
    } else {
      out.push(`<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(nw / 2).toFixed(1)}" fill="${fill}" stroke="${stroke}" stroke-width="${bw}"/>`);
    }
    const icon = n.data("icon");
    if (icon) {
      const isz = card ? 18 : nw * 0.58;
      const ix = card ? p.x - nw / 2 + 10 : p.x - isz / 2;
      out.push(`<image href="${esc(String(icon))}" x="${ix.toFixed(1)}" y="${(p.y - isz / 2).toFixed(1)}" width="${isz.toFixed(1)}" height="${isz.toFixed(1)}"/>`);
    }
    const size = num(n.style("font-size"), 12), bold = String(n.style("font-weight")) === "bold";
    const color = String(n.style("color") || opts.fg);
    const maxW = num(n.style("text-max-width"), 170);
    const text = ellipsize(String(n.data("label") ?? ""), maxW, size, bold);
    if (!text) return;
    if (card) {
      out.push(`<text x="${(p.x + 10).toFixed(1)}" y="${(p.y + size * 0.36).toFixed(1)}" text-anchor="middle" font-size="${size}"`
        + `${bold ? ' font-weight="bold"' : ""} fill="${color}">${esc(text)}</text>`);
    } else {
      const ty = p.y + nh / 2 + 5 + size * 0.9;
      const tw = measure(text, size, bold);
      out.push(`<rect x="${(p.x - tw / 2 - 1).toFixed(1)}" y="${(ty - size * 0.9).toFixed(1)}" width="${(tw + 2).toFixed(1)}" height="${(size * 1.2).toFixed(1)}" fill="${opts.bg}" fill-opacity="0.85"/>`);
      out.push(`<text x="${p.x.toFixed(1)}" y="${ty.toFixed(1)}" text-anchor="middle" font-size="${size}"`
        + `${bold ? ' font-weight="bold"' : ""} fill="${color}">${esc(text)}</text>`);
    }
  });
  out.push("</svg>");
  return out.join("\n");
}
