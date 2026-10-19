import { describe, expect, it } from "vitest";
import {
  TEXT_WIDTH, TEXT_WIDTH_LANDSCAPE, columnWidths, pdfPayload, reportDocx, reportHtml, reportOdt, workbookOds, workbookXlsx, type Report,
} from "./reportExport";

/** 讀手刻的 STORE zip：檔名 → 內容（只為了測試） */
function unzip(buf: Uint8Array): Record<string, string> {
  const out: Record<string, string> = {};
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  let p = 0;
  const dec = new TextDecoder();
  while (p + 30 <= buf.length && dv.getUint32(p, true) === 0x04034b50) {
    const size = dv.getUint32(p + 18, true), nameLen = dv.getUint16(p + 26, true);
    const name = dec.decode(buf.subarray(p + 30, p + 30 + nameLen));
    out[name] = dec.decode(buf.subarray(p + 30 + nameLen, p + 30 + nameLen + size));
    p += 30 + nameLen + size;
  }
  return out;
}

const REPORT: Report = {
  title: "198.51.100.10 改為 198.51.100.80",
  subtitle: "IP 變更評估 · 第 1 版",
  generatedAt: "2026-10-08 15:20",
  footerText: "jt-ipam · IP 變更評估",
  meta: [["決策", "有阻擋項目"], ["分析時間", "2026-10-08 09:54"], ["阻擋", "2"]],
  sections: [
    { heading: "摘要", paragraphs: ["有 2 個阻擋 <項目> & 3 個需覆核"], caption: "AI 只根據這份結果解說" },
    { heading: "資料不足", bullets: ["DNS 沒有 CNAME", "防火牆 any 規則"] },
    { heading: "影響清單", table: { cols: ["處置／嚴重度", "物件", "原因", "影響／證據"], widths: [15, 28, 43, 14],
                                    tones: ["danger", null],
                                    rows: [["阻擋\n嚴重", "DNS 紀錄\nfw-a reverse proxy", "指向舊位址", "需變更\n明確"],
                                           ["參考\n低", "防火牆規則", "涵蓋新舊位址", "僅有引用\n明確"]] } },
  ],
  note: "這是評估，不會執行任何變更。",
};

describe("column widths never exceed the text area (the ODT used to run past the right margin)", () => {
  it("add up exactly to the usable width, integers for DOCX twips", () => {
    for (const widths of [undefined, [15, 28, 43, 14], [1, 1, 1, 1], [3, 9, 5, 1]]) {
      const t = { cols: ["a", "b", "c", "d"], rows: [], widths };
      const d = columnWidths(t, TEXT_WIDTH.docx, true);
      expect(d.every(Number.isInteger)).toBe(true);
      expect(d.reduce((a, b) => a + b, 0)).toBe(TEXT_WIDTH.docx);
      expect(columnWidths(t, TEXT_WIDTH.odt).reduce((a, b) => a + b, 0)).toBeCloseTo(TEXT_WIDTH.odt, 6);
    }
  });
});

describe("report documents", () => {
  it("Word: tables are exactly the text width, the header row repeats, multi-line cells and a page-number footer", () => {
    const files = unzip(reportDocx(REPORT));
    expect(Object.keys(files)).toEqual(expect.arrayContaining(
      ["[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels", "word/footer1.xml"]));
    const doc = files["word/document.xml"];
    expect(doc).toContain("198.51.100.10 改為 198.51.100.80");
    expect(doc).toContain("有 2 個阻擋 &lt;項目&gt; &amp; 3 個需覆核");
    expect(doc).not.toContain("<項目>");
    for (const tbl of doc.match(/<w:tbl>.*?<\/w:tbl>/g) ?? []) {
      const grid = [...tbl.matchAll(/<w:gridCol w:w="(\d+)"\/>/g)].map((m) => Number(m[1]));
      expect(grid.reduce((a, b) => a + b, 0)).toBe(TEXT_WIDTH.docx);
      expect(tbl).toContain(`<w:tblW w:w="${TEXT_WIDTH.docx}" w:type="dxa"/>`);
    }
    expect(doc).toContain("<w:tblHeader/>");
    expect(doc).toContain('<w:br/>');                       // 阻擋\n嚴重
    expect(doc).toContain('w:color w:val="B42318"');         // 阻擋那一列的第一欄是紅色
    expect(doc).toContain('r:id="rIdFooter"');
    expect(files["word/footer1.xml"]).toContain('w:instr="PAGE"');
    expect(files["word/footer1.xml"]).toContain('w:instr="NUMPAGES"');
    expect(files["[Content_Types].xml"]).toContain("/word/footer1.xml");
  });

  it("OpenDocument: mimetype first, A4 page with a page-number footer, columns sum to the text width", () => {
    const files = unzip(reportOdt(REPORT));
    expect(Object.keys(files)[0]).toBe("mimetype");
    expect(files.mimetype).toBe("application/vnd.oasis.opendocument.text");
    const content = files["content.xml"];
    expect(content).toContain("<text:h");
    expect(content).toContain("DNS 沒有 CNAME");
    expect(content).toContain("<table:table-header-rows>");
    expect(content).toContain("<text:line-break/>");
    expect(content).toContain("CellTone_danger");
    for (const name of ["meta", "T3"]) {
      const cols = [...content.matchAll(new RegExp(`style:name="${name}C\\d+".*?column-width="([\\d.]+)cm"`, "g"))]
        .map((m) => Number(m[1]));
      expect(cols.length).toBeGreaterThan(0);
      expect(cols.reduce((a, b) => a + b, 0)).toBeCloseTo(TEXT_WIDTH.odt, 2);
    }
    expect(content).toContain(`style:width="${TEXT_WIDTH.odt}cm"`);
    const styles = files["styles.xml"];
    expect(styles).toContain('fo:page-width="21cm"');
    expect(styles).toContain("<text:page-number");
    expect(styles).toContain("<text:page-count>");
    expect(files["META-INF/manifest.xml"]).toContain("styles.xml");
  });

  it("PDF payload matches the backend schema (snake_case, lang limited to the three locales)", () => {
    const p = pdfPayload(REPORT, "ja-JP", "change-impact-x");
    expect(p.lang).toBe("ja-JP");
    expect(pdfPayload(REPORT, "de-DE", "x").lang).toBe("zh-TW");
    expect(p.generated_at).toBe("2026-10-08 15:20");
    expect(p.footer_text).toBe("jt-ipam · IP 變更評估");
    expect(p.sections[2].table?.tones).toEqual(["danger", null]);
    expect(p.sections[0].caption).toBe("AI 只根據這份結果解說");
    expect(p.sections[1].table).toBeNull();
    expect(Object.keys(p).sort()).toEqual(["brand", "filename", "footer_text", "generated_at", "landscape", "lang", "meta",
                                           "note", "sections", "subtitle", "title"]);
  });
});

describe("HTML report (2026-10-13: the anomaly export asks for HTML next to PDF / Word / ODT)", () => {
  it("is one self-contained page with the same outline: brand, title, meta pairs, headings, bullets, table, note", () => {
    const html = reportHtml(REPORT, "zh-TW");
    expect(html.startsWith("<!doctype html>")).toBe(true);
    expect(html).toContain('<html lang="zh-TW">');
    expect(html).toContain('<meta charset="utf-8">');
    // 不連外：沒有外部樣式表、字型或腳本
    expect(html).not.toMatch(/<script|<link |@import|url\(/i);
    expect(html).toContain("<title>198.51.100.10 改為 198.51.100.80</title>");
    expect(html).toContain("IP 變更評估 · 第 1 版");
    // 基本資料兩組一列（第三組落在下一列，空的那一半補空白格）
    expect(html.match(/<tr class="meta">/g)?.length).toBe(2);
    expect(html).toContain("<h2>影響清單</h2>");
    expect(html).toContain("<li>DNS 沒有 CNAME</li>");
    expect(html).toContain("這是評估，不會執行任何變更。");
    expect(html).toContain("jt-ipam · IP 變更評估 · 2026-10-08 15:20");
  });

  it("escapes every piece of text (host names and rule descriptions are attacker-controlled)", () => {
    const html = reportHtml({ ...REPORT, title: "<img src=x onerror=alert(1)>",
                              sections: [{ heading: "a&b", table: { cols: ["<b>"], rows: [['"><script>x</script>']] } }] }, "en-US");
    expect(html).not.toContain("<img src=x");
    expect(html).not.toContain("<script>x");
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).toContain("<h2>a&amp;b</h2>");
    expect(html).toContain("&lt;b&gt;");
  });

  it("keeps multi-line cells, the column widths and the row tone (first column only)", () => {
    const html = reportHtml(REPORT, "zh-TW");
    expect(html).toContain("阻擋<br>嚴重");
    expect(html).toContain('<col style="width:15%">');
    expect(html).toContain('<tr class="tone-danger">');
    expect(html.match(/<tr class="tone-danger">/g)?.length).toBe(1);
    // 表頭在 thead（列印時每一頁重複）
    expect(html).toMatch(/<thead><tr><th>處置／嚴重度<\/th>/);
  });

  it("an empty table prints a dash instead of a header with no rows; lang falls back to zh-TW", () => {
    const html = reportHtml({ ...REPORT, sections: [{ heading: "空", table: { cols: ["A"], rows: [] } }] }, "fr");
    expect(html).toContain('<html lang="zh-TW">');
    expect(html).toContain('<p class="muted">—</p>');
    expect(html).not.toContain("<thead>");
  });
});

describe("workbooks", () => {
  const sheets = [
    { name: "影響清單", cols: ["處置", "物件"], rows: [["需覆核", "a"], ["阻擋", "b"]] },
    { name: "資料不足/缺口:[1]", cols: ["分類", "說明"], rows: [["DNS", "x"]] },
  ];
  it("XLSX has one worksheet per sheet with safe names", () => {
    const files = unzip(workbookXlsx(sheets));
    expect(files["xl/worksheets/sheet1.xml"]).toContain("需覆核");
    expect(files["xl/worksheets/sheet2.xml"]).toContain("DNS");
    expect(files["xl/workbook.xml"]).toContain('name="影響清單"');
    // Excel 不准工作表名稱有 / : [ ] 等字元
    expect(files["xl/workbook.xml"]).toContain('name="資料不足 缺口  1"');
    expect(files["[Content_Types].xml"]).toContain("/xl/worksheets/sheet2.xml");
  });
  it("ODS has one table per sheet", () => {
    const files = unzip(workbookOds(sheets));
    expect(files.mimetype).toBe("application/vnd.oasis.opendocument.spreadsheet");
    expect((files["content.xml"].match(/<table:table /g) ?? []).length).toBe(2);
  });
});


describe("landscape", () => {
  it("turns the page and widens every table in DOCX, ODT, HTML and the PDF payload", () => {
    const r: Report = { ...REPORT, landscape: true };
    const docx = unzip(reportDocx(r))["word/document.xml"];
    expect(docx).toContain('<w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>');
    expect(docx).toContain(`<w:tblW w:w="${TEXT_WIDTH_LANDSCAPE.docx}" w:type="dxa"/>`);
    const odt = unzip(reportOdt(r));
    expect(odt["styles.xml"] + odt["content.xml"]).toContain('style:print-orientation="landscape"');
    expect(odt["content.xml"]).toContain(`style:width="${TEXT_WIDTH_LANDSCAPE.odt}cm"`);
    expect(reportHtml(r, "zh-TW")).toContain("size:A4 landscape");
    expect(pdfPayload(r, "zh-TW", "x").landscape).toBe(true);
    // 產生完回到直式：下一份沒有指定橫式的報告不受影響
    expect(unzip(reportDocx(REPORT))["word/document.xml"]).toContain('<w:pgSz w:w="11906" w:h="16838"/>');
  });
});
