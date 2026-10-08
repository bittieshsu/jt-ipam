import { describe, expect, it } from "vitest";
import cytoscape from "cytoscape";
import { ellipsize, graphToSvg } from "../graphSvg";

function cyWith() {
  const cy = cytoscape({
    headless: true, styleEnabled: true,
    style: [
      { selector: "node", style: { "background-color": "data(color)", width: "data(size)", height: "data(size)", label: "data(label)",
                                   "font-size": 12, "border-width": 2, "border-color": "#fff", "text-max-width": "170px" } },
      { selector: "edge", style: { width: 1.2, "line-color": "#c4c8cf" } },
      { selector: "edge[dashed = 1]", style: { "line-style": "dashed" } },
    ] as any,
    elements: [
      { data: { id: "zone:change_required", zone: 1, size: 300, color: "#2080f0", label: "需變更（2）" }, classes: "zone", position: { x: 0, y: 0 } },
      { data: { id: "root", label: "198.51.100.10", color: "#475569", size: 40, root: 1 }, position: { x: 0, y: 0 } },
      { data: { id: "a", label: "DNS <a&b>", color: "#2080f0", size: 28 }, position: { x: 150, y: 0 } },
      { data: { id: "b", label: "LibreNMS：laptop-07", color: "#2080f0", size: 28 }, position: { x: -150, y: 0 } },
      { data: { id: "e1", source: "a", target: "root", label: "DNS 紀錄指向", toRoot: 1 }, classes: "lbl" },
      { data: { id: "e2", source: "b", target: "root", label: "監控目標", dashed: 1 } },
    ],
  });
  return cy;
}

describe("graphToSvg (relation graph export)", () => {
  it("draws rings, nodes, edges with arrows and labels as vector SVG, escaping text", () => {
    const svg = graphToSvg(cyWith(), { bg: "#ffffff", fg: "#1f2328" });
    expect(svg.startsWith("<svg")).toBe(true);
    expect(svg).toContain('xmlns="http://www.w3.org/2000/svg"');
    expect(svg).toContain("需變更（2）");                       // 分層的標題
    expect((svg.match(/<polyline /g) ?? []).length).toBe(2);    // 兩條線
    expect((svg.match(/<polygon /g) ?? []).length).toBe(2);     // 兩個箭頭
    expect(svg).toContain('stroke-dasharray="6 4"');             // 推定的關係是虛線
    expect(svg).toContain("DNS 紀錄指向");                       // 有顯示的線上字
    expect(svg).not.toContain("監控目標");                       // 畫面上沒顯示的線上字不畫
    expect(svg).toContain("DNS &lt;a&amp;b&gt;");               // 跳脫
    expect(svg).not.toContain("<a&b>");
    expect(svg).toContain("LibreNMS：laptop-07");
    // 分層的圈只畫虛線框，不可以又被當成節點畫成實心圓（曾經整張圖被一個綠色大圓蓋住）
    expect(svg).toMatch(/<circle cx="0" cy="0" r="150" fill="none"/);
    expect(svg).not.toMatch(/<circle cx="0\.0" cy="0\.0" r="150\.0"/);
    expect((svg.match(/需變更（2）/g) ?? []).length).toBe(1);
  });
  it("truncates long names like the screen does", () => {
    const long = "very-long-hostname-that-would-overflow-the-label.example.test";
    const s = ellipsize(long, 100, 12);
    expect(s.endsWith("…")).toBe(true);
    expect(s.length).toBeLessThan(long.length);
    expect(ellipsize("short", 100, 12)).toBe("short");
  });
});
