/**
 * 報告與多工作表匯出（零相依，沿用 tableExport 的手刻 zip）。使用者 2026-10-08：
 * 「PDF、ODT、DOCX 是出報告用，XLSX、ODS 是出表格清單」；之後又說「PDF 卻出了瀏覽器列印頁，這不對；
 * 報告不好看、不專業，右邊還超出去」。
 *
 * - 報告：同一份大綱（標題、基本資料、各段落：文字／條列／表格）輸出成 DOCX、ODT；PDF 由後端排版
 *   （POST /api/v1/reports/pdf，內嵌中文字型子集），這裡只把大綱轉成它要的格式（pdfPayload）
 * - 三種格式的版面規則一致：A4 直式、標題區＋品牌色線、基本資料兩欄一組、段落標題前的色條、
 *   深色表頭（跨頁重複）、斑馬紋、9pt 表格字、欄寬加總剛好等於可用寬度（不會超出右邊界）、頁尾頁碼
 *   顏色與 backend/app/services/report_pdf.py 相同
 * - 表格：每個分頁一張工作表的 XLSX／ODS
 */
import { download, xmlEscape, zipStore } from "@/utils/tableExport";

/** 列的語氣：只影響第一欄的顏色（阻擋＝danger、需覆核＝warning） */
export type ReportTone = "danger" | "warning" | "info";
/** widths：各欄寬度比例（不給就平均）—— 原因這種長文字要寬一點，不然被擠成一條。儲存格可以用 \n 換行 */
export interface ReportTable { cols: string[]; rows: string[][]; widths?: number[]; tones?: (ReportTone | null)[] }
export interface ReportSection {
  heading: string; paragraphs?: string[]; bullets?: string[]; table?: ReportTable;
  /** 段落最後的小字說明 */
  caption?: string;
}
export interface Report {
  title: string; subtitle?: string; brand?: string; generatedAt?: string;
  meta: [string, string][]; sections: ReportSection[];
  /** 文件最後的說明（例：這是評估，不會執行任何變更） */
  note?: string;
  /** 頁尾左邊的文字（右邊是頁碼） */
  footerText?: string;
}
export interface Sheet { name: string; cols: string[]; rows: string[][] }

const enc = new TextEncoder();
const XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';

export const COLOR = {
  accent: "2F5D9B", heading: "1F3A5F", headFill: "2F4B7C", ink: "1F2937", muted: "646E7D",
  rule: "D5DBE3", labelFill: "F1F4F8", zebra: "F6F8FB",
  tone: { danger: "B42318", warning: "B54708", info: "175CD3" } as Record<ReportTone, string>,
};
/** 可用寬度：A4 寬 21 cm 扣掉左右邊界各 1.8 cm（DOCX 是 twips：11906 − 2 × 1021） */
export const TEXT_WIDTH = { docx: 9864, odt: 17.4, pdf: 174 };
const META_WIDTHS = [17, 33, 17, 33];

/** 欄寬比例換成實際寬度，加總剛好等於 total（DOCX 要整數 twips：用最大餘數法補齊） */
export function columnWidths(t: Pick<ReportTable, "cols" | "rows" | "widths">, total: number, integer = false): number[] {
  const n = Math.max(t.cols.length, t.rows[0]?.length ?? 0, 1);
  const w = t.widths && t.widths.length === n ? t.widths : Array(n).fill(1);
  const sum = w.reduce((a, b) => a + b, 0) || 1;
  const raw = w.map((x) => (x / sum) * total);
  if (!integer) {
    const out = raw.map((x) => Math.round(x * 100) / 100);
    out[out.length - 1] = Math.round((total - out.slice(0, -1).reduce((a, b) => a + b, 0)) * 100) / 100;
    return out;
  }
  const floors = raw.map(Math.floor);
  let left = total - floors.reduce((a, b) => a + b, 0);
  raw.map((x, i) => [x - floors[i], i] as const).sort((a, b) => b[0] - a[0]).forEach(([, i]) => {
    if (left > 0) { floors[i] += 1; left -= 1; }
  });
  return floors;
}
const metaRows = (meta: [string, string][]): string[][] => {
  const rows: string[][] = [];
  for (let i = 0; i < meta.length; i += 2) rows.push([...meta[i], ...(meta[i + 1] ?? ["", ""])]);
  return rows;
};

// ── DOCX ──
const W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
  + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"';
interface RunOpts { bold?: boolean; size?: number; color?: string }
function wRunText(text: string, o: RunOpts = {}): string {
  const rpr = `<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" w:eastAsia="Microsoft JhengHei"/>`
    + (o.bold ? "<w:b/><w:bCs/>" : "") + `<w:color w:val="${o.color ?? COLOR.ink}"/>`
    + `<w:sz w:val="${o.size ?? 20}"/><w:szCs w:val="${o.size ?? 20}"/><w:lang w:eastAsia="zh-TW"/></w:rPr>`;
  // 儲存格裡的 \n：同一段內換行
  return `<w:r>${rpr}${text.split("\n").map((l, i) => `${i ? "<w:br/>" : ""}<w:t xml:space="preserve">${xmlEscape(l)}</w:t>`).join("")}</w:r>`;
}
interface ParaOpts extends RunOpts { before?: number; after?: number; ind?: string; border?: string; shade?: string;
                                     keepNext?: boolean; tabs?: string }
function wPara(text: string, o: ParaOpts = {}, inner?: string): string {
  const ppr = "<w:pPr>" + (o.keepNext ? "<w:keepNext/>" : "") + (o.border ? `<w:pBdr>${o.border}</w:pBdr>` : "")
    + (o.shade ? `<w:shd w:val="clear" w:color="auto" w:fill="${o.shade}"/>` : "") + (o.tabs ?? "")
    + `<w:spacing w:before="${o.before ?? 0}" w:after="${o.after ?? 80}" w:line="276" w:lineRule="auto"/>`
    + (o.ind ?? "") + "</w:pPr>";
  return `<w:p>${ppr}${inner ?? wRunText(text, o)}</w:p>`;
}
const wBorder = (side: string, color = COLOR.rule, sz = 4) =>
  `<w:${side} w:val="single" w:sz="${sz}" w:space="0" w:color="${color}"/>`;
const wNil = (side: string) => `<w:${side} w:val="nil"/>`;

function wTable(t: ReportTable, opts: { meta?: boolean } = {}): string {
  const tw = columnWidths(t, TEXT_WIDTH.docx, true);
  const cell = (s: string, i: number, kind: "head" | "body" | "zebra" | "label", tone?: ReportTone | null) => {
    const fill = kind === "head" ? COLOR.headFill : kind === "zebra" ? COLOR.zebra : kind === "label" ? COLOR.labelFill : "";
    const run = kind === "head" ? { bold: true, size: 18, color: "FFFFFF" }
      : kind === "label" ? { size: 18, color: COLOR.muted }
      : i === 0 && tone ? { bold: true, size: 18, color: COLOR.tone[tone] } : { size: 18 };
    return `<w:tc><w:tcPr><w:tcW w:w="${tw[i]}" w:type="dxa"/>`
      + (fill ? `<w:shd w:val="clear" w:color="auto" w:fill="${fill}"/>` : "")
      + `</w:tcPr><w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>${wRunText(s, run)}</w:p></w:tc>`;
  };
  const rows: string[] = [];
  if (opts.meta) {
    for (const r of t.rows) rows.push(`<w:tr><w:trPr><w:cantSplit/></w:trPr>${r.map((c, i) => cell(c, i, i % 2 === 0 ? "label" : "body")).join("")}</w:tr>`);
  } else {
    rows.push(`<w:tr><w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>${t.cols.map((c, i) => cell(c, i, "head")).join("")}</w:tr>`);
    t.rows.forEach((r, ri) => rows.push(`<w:tr><w:trPr><w:cantSplit/></w:trPr>`
      + r.map((c, i) => cell(c, i, ri % 2 ? "zebra" : "body", t.tones?.[ri])).join("") + "</w:tr>"));
  }
  const borders = opts.meta
    ? ["top", "left", "bottom", "right", "insideH", "insideV"].map((s) => wBorder(s)).join("")
    : [wBorder("top"), wNil("left"), wBorder("bottom"), wNil("right"), wBorder("insideH"), wNil("insideV")].join("");
  return `<w:tbl><w:tblPr><w:tblW w:w="${TEXT_WIDTH.docx}" w:type="dxa"/><w:tblLayout w:type="fixed"/>`
    + `<w:tblBorders>${borders}</w:tblBorders>`
    + `<w:tblCellMar><w:top w:w="50" w:type="dxa"/><w:left w:w="100" w:type="dxa"/><w:bottom w:w="50" w:type="dxa"/>`
    + `<w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr>`
    + `<w:tblGrid>${tw.map((w) => `<w:gridCol w:w="${w}"/>`).join("")}</w:tblGrid>${rows.join("")}</w:tbl>`
    + wPara("", { after: 80, size: 8 });
}

export function reportDocx(r: Report): Uint8Array {
  const parts: string[] = [
    wPara(r.brand ?? "jt-ipam", { bold: true, size: 18, color: COLOR.accent, after: 40 }),
    wPara(r.title, { bold: true, size: 36, after: 40 }),
  ];
  if (r.subtitle) parts.push(wPara(r.subtitle, { size: 20, color: COLOR.muted, after: 60 }));
  parts.push(wPara("", { size: 4, after: 200, border: `<w:bottom w:val="single" w:sz="12" w:space="1" w:color="${COLOR.accent}"/>` }));
  if (r.meta.length) {
    parts.push(wTable({ cols: ["", "", "", ""], rows: metaRows(r.meta), widths: META_WIDTHS }, { meta: true }));
  }
  for (const s of r.sections) {
    parts.push(wPara(s.heading, { bold: true, size: 25, color: COLOR.heading, before: 240, after: 100, keepNext: true,
                                  border: `<w:left w:val="single" w:sz="24" w:space="6" w:color="${COLOR.accent}"/>`,
                                  ind: '<w:ind w:left="120"/>' }));
    for (const p of s.paragraphs ?? []) parts.push(wPara(p));
    for (const b of s.bullets ?? []) {
      parts.push(wPara("", { ind: '<w:ind w:left="360" w:hanging="240"/>' },
                       wRunText("• ", { color: COLOR.accent }) + wRunText(b)));
    }
    if (s.table) parts.push(s.table.rows.length ? wTable(s.table) : wPara("—", { color: COLOR.muted }));
    if (s.caption) parts.push(wPara(s.caption, { size: 17, color: COLOR.muted }));
  }
  if (r.note) parts.push(wPara(r.note, { size: 17, color: COLOR.muted, before: 240, shade: COLOR.zebra }));
  const footLeft = [r.footerText ?? r.brand ?? "jt-ipam", r.generatedAt].filter(Boolean).join(" · ");
  const fRun = (s: string) => wRunText(s, { size: 16, color: COLOR.muted });
  const field = (instr: string) => `<w:fldSimple w:instr="${instr}">${fRun("1")}</w:fldSimple>`;
  const footer = `${XML}<w:ftr ${W_NS}>`
    + wPara("", { tabs: `<w:tabs><w:tab w:val="right" w:pos="${TEXT_WIDTH.docx}"/></w:tabs>`, after: 0,
                  border: `<w:top w:val="single" w:sz="4" w:space="4" w:color="${COLOR.rule}"/>` },
            fRun(footLeft) + `<w:r><w:tab/></w:r>` + field("PAGE") + fRun(" / ") + field("NUMPAGES"))
    + "</w:ftr>";
  const doc = `${XML}<w:document ${W_NS}><w:body>${parts.join("")}`
    + `<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/>`
    + `<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="907" w:right="1021" w:bottom="1021" w:left="1021"`
    + ` w:header="454" w:footer="454" w:gutter="0"/></w:sectPr></w:body></w:document>`;
  const types = `${XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">`
    + `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>`
    + `<Default Extension="xml" ContentType="application/xml"/>`
    + `<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>`
    + `<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>`
    + `</Types>`;
  const rels = `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
    + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>`
    + `</Relationships>`;
  const docRels = `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
    + `<Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>`
    + `</Relationships>`;
  return zipStore([
    { name: "[Content_Types].xml", data: enc.encode(types) },
    { name: "_rels/.rels", data: enc.encode(rels) },
    { name: "word/document.xml", data: enc.encode(doc) },
    { name: "word/_rels/document.xml.rels", data: enc.encode(docRels) },
    { name: "word/footer1.xml", data: enc.encode(footer) },
  ]);
}

// ── ODT ──
const ODF_NS = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
  + 'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
  + 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
  + 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
  + 'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.2"';
const manifest = (media: string, extra: string[] = []) => `<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="${media}"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
${extra.map((f) => ` <manifest:file-entry manifest:full-path="${f}" manifest:media-type="text/xml"/>`).join("\n")}
</manifest:manifest>`;

const odtText = (s: string) => s.split("\n").map(xmlEscape).join("<text:line-break/>");
const FONT = `fo:font-family="'Liberation Sans', Arial, sans-serif" style:font-family-asian="'Noto Sans CJK TC', 'Microsoft JhengHei', sans-serif"`;
const tp = (size: string, color = COLOR.ink, bold = false) =>
  `<style:text-properties ${FONT} fo:font-size="${size}" style:font-size-asian="${size}" fo:color="#${color}"`
  + (bold ? ' fo:font-weight="bold" style:font-weight-asian="bold"' : "") + "/>";

/** 每張表的欄寬要各自一組欄樣式（ODF 的欄寬寫在 automatic-styles） */
function odtColStyles(name: string, widths: number[]): string {
  return widths.map((w, i) => `<style:style style:name="${name}C${i}" style:family="table-column">`
    + `<style:table-column-properties style:column-width="${w.toFixed(2)}cm"/></style:style>`).join("");
}
function odtTable(t: ReportTable, name: string, meta = false): string {
  const n = columnWidths(t, TEXT_WIDTH.odt).length;
  const cell = (s: string, cs: string, ps: string) => `<table:table-cell table:style-name="${cs}" office:value-type="string">`
    + `<text:p text:style-name="${ps}">${odtText(s)}</text:p></table:table-cell>`;
  const cols = Array.from({ length: n }, (_x, i) => `<table:table-column table:style-name="${name}C${i}"/>`).join("");
  if (meta) {
    return `<table:table table:name="${name}" table:style-name="Tbl">${cols}`
      + t.rows.map((r) => `<table:table-row>${r.map((c, i) => cell(c, i % 2 ? "MetaCell" : "MetaLabel",
                                                               i % 2 ? "CellText" : "CellLabel")).join("")}</table:table-row>`).join("")
      + "</table:table>";
  }
  return `<table:table table:name="${name}" table:style-name="Tbl">${cols}`
    + `<table:table-header-rows><table:table-row>${t.cols.map((c) => cell(c, "HeadCell", "CellHead")).join("")}</table:table-row></table:table-header-rows>`
    + t.rows.map((r, ri) => `<table:table-row>${r.map((c, i) => cell(c, ri % 2 ? "CellZ" : "Cell",
      i === 0 && t.tones?.[ri] ? `CellTone_${t.tones[ri]}` : "CellText")).join("")}</table:table-row>`).join("")
    + "</table:table>";
}

export function reportOdt(r: Report): Uint8Array {
  const tables: [string, number[]][] = [];
  if (r.meta.length) tables.push(["meta", columnWidths({ cols: ["", "", "", ""], rows: [], widths: META_WIDTHS }, TEXT_WIDTH.odt)]);
  r.sections.forEach((s, i) => { if (s.table?.rows.length) tables.push([`T${i + 1}`, columnWidths(s.table, TEXT_WIDTH.odt)]); });
  const cellBox = (bg = "", all = false) => `<style:table-cell-properties fo:padding-top="0.07cm" fo:padding-bottom="0.07cm"`
    + ` fo:padding-left="0.15cm" fo:padding-right="0.15cm"`
    + (all ? ` fo:border="0.5pt solid #${COLOR.rule}"` : ` fo:border-top="none" fo:border-left="none" fo:border-right="none" fo:border-bottom="0.5pt solid #${COLOR.rule}"`)
    + (bg ? ` fo:background-color="#${bg}"` : "") + "/>";
  const styles = `<office:automatic-styles>
 <style:style style:name="Brand" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.08cm"/>${tp("9pt", COLOR.accent, true)}</style:style>
 <style:style style:name="Title" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.08cm"/>${tp("18pt", COLOR.ink, true)}</style:style>
 <style:style style:name="Subtitle" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.1cm"/>${tp("10pt", COLOR.muted)}</style:style>
 <style:style style:name="Rule" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.4cm" fo:border-bottom="1.5pt solid #${COLOR.accent}" fo:padding="0cm"/>${tp("2pt")}</style:style>
 <style:style style:name="H" style:family="paragraph"><style:paragraph-properties fo:margin-top="0.45cm" fo:margin-bottom="0.18cm" fo:keep-with-next="always" fo:border-left="3pt solid #${COLOR.accent}" fo:padding-left="0.2cm"/>${tp("12.5pt", COLOR.heading, true)}</style:style>
 <style:style style:name="Body" style:family="paragraph"><style:paragraph-properties fo:margin-bottom="0.12cm" fo:line-height="130%"/>${tp("10pt")}</style:style>
 <style:style style:name="Bullet" style:family="paragraph"><style:paragraph-properties fo:margin-left="0.6cm" fo:text-indent="-0.35cm" fo:margin-bottom="0.08cm" fo:line-height="130%"/>${tp("10pt")}</style:style>
 <style:style style:name="Caption" style:family="paragraph"><style:paragraph-properties fo:margin-top="0.08cm"/>${tp("8.5pt", COLOR.muted)}</style:style>
 <style:style style:name="Note" style:family="paragraph"><style:paragraph-properties fo:margin-top="0.4cm" fo:padding="0.2cm" fo:background-color="#${COLOR.zebra}"/>${tp("8.5pt", COLOR.muted)}</style:style>
 <style:style style:name="Gap" style:family="paragraph">${tp("4pt")}</style:style>
 <style:style style:name="CellText" style:family="paragraph">${tp("9pt")}</style:style>
 <style:style style:name="CellLabel" style:family="paragraph">${tp("9pt", COLOR.muted)}</style:style>
 <style:style style:name="CellHead" style:family="paragraph">${tp("9pt", "FFFFFF", true)}</style:style>
 ${(Object.keys(COLOR.tone) as ReportTone[]).map((k) => `<style:style style:name="CellTone_${k}" style:family="paragraph">${tp("9pt", COLOR.tone[k], true)}</style:style>`).join("")}
 <style:style style:name="Cell" style:family="table-cell">${cellBox()}</style:style>
 <style:style style:name="CellZ" style:family="table-cell">${cellBox(COLOR.zebra)}</style:style>
 <style:style style:name="HeadCell" style:family="table-cell">${cellBox(COLOR.headFill)}</style:style>
 <style:style style:name="MetaLabel" style:family="table-cell">${cellBox(COLOR.labelFill, true)}</style:style>
 <style:style style:name="MetaCell" style:family="table-cell">${cellBox("", true)}</style:style>
 <style:style style:name="Tbl" style:family="table"><style:table-properties style:width="${TEXT_WIDTH.odt}cm" table:align="left" fo:margin-bottom="0.2cm" table:border-model="collapsing"/></style:style>
 ${tables.map(([n, w]) => odtColStyles(n, w)).join("")}
</office:automatic-styles>`;
  const body: string[] = [
    `<text:p text:style-name="Brand">${xmlEscape(r.brand ?? "jt-ipam")}</text:p>`,
    `<text:h text:outline-level="1" text:style-name="Title">${xmlEscape(r.title)}</text:h>`,
  ];
  if (r.subtitle) body.push(`<text:p text:style-name="Subtitle">${xmlEscape(r.subtitle)}</text:p>`);
  body.push('<text:p text:style-name="Rule"/>');
  if (r.meta.length) {
    body.push(odtTable({ cols: ["", "", "", ""], rows: metaRows(r.meta), widths: META_WIDTHS }, "meta", true),
              '<text:p text:style-name="Gap"/>');
  }
  r.sections.forEach((s, i) => {
    body.push(`<text:h text:outline-level="2" text:style-name="H">${xmlEscape(s.heading)}</text:h>`);
    for (const p of s.paragraphs ?? []) body.push(`<text:p text:style-name="Body">${odtText(p)}</text:p>`);
    for (const b of s.bullets ?? []) body.push(`<text:p text:style-name="Bullet">• ${odtText(b)}</text:p>`);
    if (s.table) body.push(s.table.rows.length ? odtTable(s.table, `T${i + 1}`) : `<text:p text:style-name="Body">—</text:p>`);
    if (s.caption) body.push(`<text:p text:style-name="Caption">${odtText(s.caption)}</text:p>`);
  });
  if (r.note) body.push(`<text:p text:style-name="Note">${odtText(r.note)}</text:p>`);
  const content = `<?xml version="1.0" encoding="UTF-8"?><office:document-content ${ODF_NS}>${styles}`
    + `<office:body><office:text>${body.join("")}</office:text></office:body></office:document-content>`;
  const footLeft = [r.footerText ?? r.brand ?? "jt-ipam", r.generatedAt].filter(Boolean).join(" · ");
  // 版面（A4、邊界）與頁尾頁碼要放在 styles.xml 的主版頁面
  const stylesXml = `<?xml version="1.0" encoding="UTF-8"?><office:document-styles ${ODF_NS}>
<office:styles>
 <style:default-style style:family="paragraph">${tp("10pt")}</style:default-style>
 <style:style style:name="Footer" style:family="paragraph"><style:paragraph-properties fo:border-top="0.5pt solid #${COLOR.rule}" fo:padding-top="0.1cm"><style:tab-stops><style:tab-stop style:position="${TEXT_WIDTH.odt}cm" style:type="right"/></style:tab-stops></style:paragraph-properties>${tp("8pt", COLOR.muted)}</style:style>
</office:styles>
<office:automatic-styles>
 <style:page-layout style:name="pm1"><style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" style:print-orientation="portrait" fo:margin-top="1.5cm" fo:margin-bottom="1.2cm" fo:margin-left="1.8cm" fo:margin-right="1.8cm"/><style:footer-style><style:header-footer-properties fo:min-height="0.5cm" fo:margin-top="0.3cm"/></style:footer-style></style:page-layout>
</office:automatic-styles>
<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="pm1"><style:footer><text:p text:style-name="Footer">${xmlEscape(footLeft)}<text:tab/><text:page-number text:select-page="current">1</text:page-number> / <text:page-count>1</text:page-count></text:p></style:footer></style:master-page></office:master-styles>
</office:document-styles>`;
  const media = "application/vnd.oasis.opendocument.text";
  return zipStore([
    { name: "mimetype", data: enc.encode(media) },
    { name: "content.xml", data: enc.encode(content) },
    { name: "styles.xml", data: enc.encode(stylesXml) },
    { name: "META-INF/manifest.xml", data: enc.encode(manifest(media, ["styles.xml"])) },
  ]);
}

// ── PDF：後端排版（POST /api/v1/reports/pdf）──
export type ReportLang = "zh-TW" | "en-US" | "ja-JP";
/** 轉成後端 ReportIn 的形狀（欄位名稱 snake_case；語系決定用中文還是日文字面） */
export function pdfPayload(r: Report, lang: string, filename: string) {
  const l: ReportLang = lang === "en-US" || lang === "ja-JP" ? lang : "zh-TW";
  return {
    title: r.title, subtitle: r.subtitle ?? "", brand: r.brand ?? "jt-ipam", generated_at: r.generatedAt ?? "",
    meta: r.meta, note: r.note ?? "", footer_text: r.footerText ?? "", lang: l, filename,
    sections: r.sections.map((s) => ({
      heading: s.heading, paragraphs: s.paragraphs ?? [], bullets: s.bullets ?? [], caption: s.caption ?? "",
      table: s.table ? { cols: s.table.cols, rows: s.table.rows, widths: s.table.widths ?? null,
                         tones: s.table.tones ?? null } : null,
    })),
  };
}

// ── 多工作表：XLSX ──
/** Excel 的工作表名稱：最多 31 字，不可以有 \ / ? * [ ] : */
function sheetName(s: string, i: number): string {
  const n = s.replace(/[\\/?*[\]:]/g, " ").slice(0, 31).trim();
  return n || `Sheet${i + 1}`;
}

export function workbookXlsx(sheets: Sheet[]): Uint8Array {
  const cellXml = (v: string) => `<c t="inlineStr"><is><t xml:space="preserve">${xmlEscape(v)}</t></is></c>`;
  const rowXml = (vals: string[]) => `<row>${vals.map(cellXml).join("")}</row>`;
  const files: { name: string; data: Uint8Array }[] = [];
  const names = sheets.map((s, i) => sheetName(s.name, i));
  const types = `${XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">`
    + `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>`
    + `<Default Extension="xml" ContentType="application/xml"/>`
    + `<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>`
    + sheets.map((_s, i) => `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join("")
    + `</Types>`;
  const workbook = `${XML}<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"`
    + ` xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>`
    + names.map((n, i) => `<sheet name="${xmlEscape(n)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join("")
    + `</sheets></workbook>`;
  const wbRels = `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
    + sheets.map((_s, i) => `<Relationship Id="rId${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`).join("")
    + `</Relationships>`;
  const rootRels = `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
    + `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>`
    + `</Relationships>`;
  files.push({ name: "[Content_Types].xml", data: enc.encode(types) },
             { name: "_rels/.rels", data: enc.encode(rootRels) },
             { name: "xl/workbook.xml", data: enc.encode(workbook) },
             { name: "xl/_rels/workbook.xml.rels", data: enc.encode(wbRels) });
  sheets.forEach((s, i) => {
    const xml = `${XML}<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>`
      + [rowXml(s.cols), ...s.rows.map(rowXml)].join("") + `</sheetData></worksheet>`;
    files.push({ name: `xl/worksheets/sheet${i + 1}.xml`, data: enc.encode(xml) });
  });
  return zipStore(files);
}

// ── 多工作表：ODS ──
export function workbookOds(sheets: Sheet[]): Uint8Array {
  const cell = (s: string) => `<table:table-cell office:value-type="string"><text:p>${xmlEscape(s)}</text:p></table:table-cell>`;
  const tables = sheets.map((s, i) => `<table:table table:name="${xmlEscape(sheetName(s.name, i))}">`
    + [s.cols, ...s.rows].map((r) => `<table:table-row>${r.map(cell).join("")}</table:table-row>`).join("")
    + `</table:table>`).join("");
  const content = `<?xml version="1.0" encoding="UTF-8"?><office:document-content ${ODF_NS}>`
    + `<office:body><office:spreadsheet>${tables}</office:spreadsheet></office:body></office:document-content>`;
  const media = "application/vnd.oasis.opendocument.spreadsheet";
  return zipStore([
    { name: "mimetype", data: enc.encode(media) },
    { name: "content.xml", data: enc.encode(content) },
    { name: "META-INF/manifest.xml", data: enc.encode(manifest(media)) },
  ]);
}

export const MIME = {
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  odt: "application/vnd.oasis.opendocument.text",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ods: "application/vnd.oasis.opendocument.spreadsheet",
} as const;

export function saveBytes(filename: string, bytes: Uint8Array, mime: string): void {
  download(filename, bytes, mime);
}
