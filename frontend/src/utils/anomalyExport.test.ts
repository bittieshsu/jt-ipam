import { describe, expect, it } from "vitest";
import {
  PDF_MAX, anomalyReport, anomalySheets, clipForPdf, pdfProblem, suggestWidths, type AnomalyExport,
} from "./anomalyExport";

const X: AnomalyExport = {
  title: "異常偵測報告",
  generatedAt: "2026-10-13 18:00",
  meta: [["上次執行", "2026-10-13 17:55"], ["篩選", "198.51.100"]],
  note: "依你的權限過濾。",
  footerText: "jt-ipam · 異常偵測",
  overview: { heading: "總覽", colCategory: "分類", colCount: "筆數", colSummary: "說明" },
  tables: [
    { key: "ip_conflicts", title: "IP 衝突", summary: "同一個 IP 被兩台以上的機器使用。",
      cols: ["IP", "MAC"], rows: [["198.51.100.7", "02:00:00:00:00:01\n02:00:00:00:00:02"]], total: 3 },
    { key: "mac_drifts", title: "MAC 變動", summary: "換了埠。", cols: ["MAC"], rows: [], total: 0 },
  ],
};

describe("anomaly export (2026-10-13)", () => {
  it("report: an overview of every category (with matched/total when filtered), then a section per category that has rows", () => {
    const r = anomalyReport(X);
    expect(r.title).toBe("異常偵測報告");
    expect(r.meta).toEqual(X.meta);
    expect(r.sections[0].heading).toBe("總覽");
    expect(r.sections[0].table?.rows).toEqual([["IP 衝突", "1/3", "同一個 IP 被兩台以上的機器使用。"],
                                               ["MAC 變動", "0", "換了埠。"]]);
    // 沒有資料的分類只出現在總覽裡，不印一個空表格
    expect(r.sections.map((s) => s.heading)).toEqual(["總覽", "IP 衝突 (1/3)"]);
    expect(r.sections[1].paragraphs).toEqual(["同一個 IP 被兩台以上的機器使用。"]);
    expect(r.sections[1].table?.cols).toEqual(["IP", "MAC"]);
    expect(r.note).toBe("依你的權限過濾。");
  });

  it("sheets: an overview sheet plus one sheet per category that has rows", () => {
    const s = anomalySheets(X);
    expect(s.map((x) => x.name)).toEqual(["總覽", "IP 衝突"]);
    expect(s[0].cols).toEqual(["分類", "筆數", "說明"]);
    expect(s[1].rows[0][1]).toContain("\n");
  });

  it("column widths follow the longest line (CJK counts double) and stay within a sane range", () => {
    const w = suggestWidths(["IP", "說明"], [["198.51.100.7", "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"]]);
    expect(w[1]).toBeGreaterThan(w[0]);
    const tiny = suggestWidths(["A"], [["x"]]);
    expect(tiny[0]).toBeGreaterThanOrEqual(6);
    const huge = suggestWidths(["A"], [["x".repeat(500)]]);
    expect(huge[0]).toBeLessThanOrEqual(30);
    // 短欄不被長文字欄擠到折行：IP 欄至少是長文字欄的四成
    const [ip, mac] = suggestWidths(["IP", "MAC"], [["198.51.100.7", "02:00:00:00:00:01（ExampleCorp）· 掃描代理 · 2026-10-13 18:00"]]);
    expect(ip / mac).toBeGreaterThan(0.4);
  });

  it("PDF limits are checked before sending (the server would answer 422 with no hint)", () => {
    expect(pdfProblem(anomalyReport(X))).toBeNull();
    const wide = { ...X, tables: [{ ...X.tables[0], cols: Array(13).fill("c"), rows: [Array(13).fill("v")] }] };
    expect(pdfProblem(anomalyReport(wide))).toEqual({ kind: "cols", title: "IP 衝突", n: 13 });
    const rows = Array.from({ length: PDF_MAX.cells / 2 + 1 }, () => ["a", "b"]);
    const big = { ...X, tables: [{ ...X.tables[0], rows, total: rows.length }] };
    expect(pdfProblem(anomalyReport(big))?.kind).toBe("rows");
  });

  it("a cell longer than the server allows is shortened with a visible ellipsis", () => {
    const long = { ...X, tables: [{ ...X.tables[0], rows: [["x".repeat(PDF_MAX.cell + 50), "y"]] }] };
    const r = clipForPdf(anomalyReport(long));
    const cell = r.sections[1].table!.rows[0][0];
    expect(cell.length).toBeLessThanOrEqual(PDF_MAX.cell);
    expect(cell.endsWith("…")).toBe(true);
  });
});
