/**
 * 對外開放服務清單的匯出輔助（使用者 2026-10-19）。
 *
 * AI 分析回來的是 Markdown：PDF／DOCX／ODT 不認 Markdown，要先拆成報告的段落 ——
 * 「## 標題」一節、條列是 bullets、其餘是段落，粗體記號拿掉（留下文字）。
 */
import type { ReportSection } from "@/utils/reportExport";

const stripInline = (s: string) => s.replace(/\*\*(.+?)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1").trim();

export function markdownSections(md: string, fallbackHeading: string): ReportSection[] {
  const out: ReportSection[] = [];
  let cur: ReportSection | null = null;
  const ensure = () => { if (!cur) { cur = { heading: fallbackHeading }; out.push(cur); } return cur; };
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const h = line.match(/^#{1,6}\s+(.*)$/);
    if (h) { cur = { heading: `${fallbackHeading}：${stripInline(h[1])}` }; out.push(cur); continue; }
    const b = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    const sec = ensure();
    if (b) (sec.bullets ??= []).push(stripInline(b[1]));
    else (sec.paragraphs ??= []).push(stripInline(line));
  }
  return out;
}
