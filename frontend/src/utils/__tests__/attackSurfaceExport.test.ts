import { describe, expect, it } from "vitest";
import { markdownSections } from "@/utils/attackSurfaceExport";
import { reportMarkdown } from "@/utils/reportExport";

describe("markdownSections", () => {
  it("splits headings, bullets and paragraphs and drops inline marks", () => {
    const s = markdownSections("開頭一句\n## 摘要\n**整體**偏高。\n## 主要風險\n- SSH `22` 對外\n1. RDP", "AI 分析");
    expect(s.map((x) => x.heading)).toEqual(["AI 分析", "AI 分析：摘要", "AI 分析：主要風險"]);
    expect(s[0].paragraphs).toEqual(["開頭一句"]);
    expect(s[1].paragraphs).toEqual(["整體偏高。"]);
    expect(s[2].bullets).toEqual(["SSH 22 對外", "RDP"]);
  });
});

describe("reportMarkdown", () => {
  it("writes meta, sections and escaped tables", () => {
    const md = reportMarkdown({
      title: "清單", meta: [["產生時間", "2026-10-19"]],
      sections: [{ heading: "IP", table: { cols: ["IP", "名稱"], rows: [["192.0.2.1", "a|b\nc"]] } },
                 { heading: "AI", bullets: ["x"] }],
      note: "備註",
    });
    expect(md).toContain("# 清單");
    expect(md).toContain("| 產生時間 | 2026-10-19 |");
    expect(md).toContain("| 192.0.2.1 | a\\|b<br>c |");
    expect(md).toContain("## AI\n\n- x");
    expect(md).toContain("> 備註");
  });
});
