/**
 * 異常偵測的匯出（使用者 2026-10-13：「要有匯出功能 pdf html csv odt docx ods xlsx」）。
 *
 * 畫面那一端把每個分類轉成純文字表格（可見欄位、使用者排的欄位順序、目前的篩選），這裡只負責組成：
 *  - 報告大綱（PDF／DOCX／ODT／HTML 共用，見 reportExport）：總覽（每個分類幾筆）＋每個有資料的分類一段
 *  - 工作表（XLSX／ODS）：總覽一張＋每個有資料的分類一張
 * PDF 由後端排版、有大小上限（backend/app/schemas/reports.py），送出前先檢查，超過就講清楚要怎麼辦，
 * 不讓使用者只看到一個 422。
 */
import type { Report, ReportSection, Sheet } from "@/utils/reportExport";

export interface AnomalyTable {
  key: string;
  title: string;
  /** 這個分類的一句話說明 */
  summary: string;
  cols: string[];
  /** 已篩選的列（儲存格可以用 \n 換行） */
  rows: string[][];
  /** 篩選前的總數 */
  total: number;
}
export interface AnomalyExport {
  title: string;
  generatedAt: string;
  meta: [string, string][];
  note: string;
  footerText: string;
  overview: { heading: string; colCategory: string; colCount: string; colSummary: string };
  tables: AnomalyTable[];
}

/** 與 backend/app/schemas/reports.py 的上限一致 */
export const PDF_MAX = { cols: 12, rows: 3_000, cells: 12_000, cell: 4_000 };

/** 篩選過就寫「符合／全部」，沒篩選就只寫總數 */
function countOf(t: AnomalyTable): string {
  return t.rows.length === t.total ? String(t.total) : `${t.rows.length}/${t.total}`;
}

/** 顯示寬度：中日韓文字算兩格；英數字算 1.2 格（多數字型的數字比半個中文字寬，IP 位址才不會被低估） */
function weight(s: string): number {
  let n = 0;
  for (const ch of s) n += /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/.test(ch) ? 2 : 1.2;
  return n;
}

/**
 * 欄寬比例：每欄最長的一行（含標題）加上內距，限制在 6～30。
 * 上限壓低是為了讓短欄（IP、狀態）不被長文字欄擠到折行：「198.51.100.7」折成兩行就讀不出來了；
 * 長文字本來就會換行，少給一點寬度只是多一行。
 */
export function suggestWidths(cols: string[], rows: string[][]): number[] {
  return cols.map((c, i) => {
    let w = weight(c);
    for (const r of rows.slice(0, 500)) {
      for (const line of (r[i] ?? "").split("\n")) w = Math.max(w, weight(line));
    }
    return Math.min(30, Math.max(6, w + 2));
  });
}

export function anomalyReport(x: AnomalyExport): Report {
  const sections: ReportSection[] = [{
    heading: x.overview.heading,
    table: { cols: [x.overview.colCategory, x.overview.colCount, x.overview.colSummary], widths: [26, 10, 64],
             rows: x.tables.map((t) => [t.title, countOf(t), t.summary]) },
  }];
  for (const t of x.tables) {
    if (!t.rows.length) continue;
    sections.push({ heading: `${t.title} (${countOf(t)})`, paragraphs: [t.summary],
                    table: { cols: t.cols, rows: t.rows, widths: suggestWidths(t.cols, t.rows) } });
  }
  return { title: x.title, generatedAt: x.generatedAt, footerText: x.footerText, meta: x.meta, sections, note: x.note };
}

export function anomalySheets(x: AnomalyExport): Sheet[] {
  return [
    { name: x.overview.heading, cols: [x.overview.colCategory, x.overview.colCount, x.overview.colSummary],
      rows: x.tables.map((t) => [t.title, countOf(t), t.summary]) },
    ...x.tables.filter((t) => t.rows.length).map((t) => ({ name: t.title, cols: t.cols, rows: t.rows })),
  ];
}

export type PdfProblem =
  | { kind: "cols"; title: string; n: number }
  | { kind: "rows"; title: string; n: number }
  | { kind: "cells"; n: number };

/** 後端排版的上限：欄數、單表列數、全部儲存格數。回傳第一個超過的（null＝可以送） */
export function pdfProblem(r: Report): PdfProblem | null {
  let cells = 0;
  for (const s of r.sections) {
    if (!s.table) continue;
    const title = s.heading.replace(/ \([^()]*\)$/, "");   // 去掉標題後面的「(筆數)」
    if (s.table.cols.length > PDF_MAX.cols) return { kind: "cols", title, n: s.table.cols.length };
    if (s.table.rows.length > PDF_MAX.rows) return { kind: "rows", title, n: s.table.rows.length };
    cells += s.table.rows.reduce((a, row) => a + row.length, 0);
  }
  return cells > PDF_MAX.cells ? { kind: "cells", n: cells } : null;
}

/** 單一儲存格超過後端上限（實務上幾乎不會）：截短並留下看得見的「…」 */
export function clipForPdf(r: Report): Report {
  const clip = (s: string) => (s.length > PDF_MAX.cell ? `${s.slice(0, PDF_MAX.cell - 1)}…` : s);
  return { ...r, sections: r.sections.map((s) => (s.table
    ? { ...s, table: { ...s.table, rows: s.table.rows.map((row) => row.map(clip)) } }
    : s)) };
}
