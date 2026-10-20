/**
 * Markdown 報告的表格儲存格跳脫（CodeQL js/incomplete-sanitization，GitHub code scanning #54）。
 * 以前只把 `|` 換成 `\|`、沒先跳脫反斜線：內容本身是 `a\|b` 時會變成 `a\\|b` —— `\\` 成了一個字面反斜線，
 * 後面的 `|` 又變回欄位分隔，整列錯位。反斜線要先處理。
 */
import { describe, expect, it } from "vitest";
import { reportMarkdown } from "../reportExport";

describe("reportMarkdown 表格儲存格", () => {
  it("反斜線、直線、換行都跳脫，欄位數不變", () => {
    const md = reportMarkdown({
      title: "t", meta: [],
      sections: [{ heading: "h", table: { cols: ["a", "b"], rows: [["x\\|y", "1\n2"], ["C:\\path", "|"]] } }],
    } as any);
    const rows = md.split("\n").filter((l) => l.startsWith("| x") || l.startsWith("| C:"));
    expect(rows[0]).toBe("| x\\\\\\|y | 1<br>2 |");
    expect(rows[1]).toBe("| C:\\\\path | \\| |");
    for (const r of rows) {
      // 沒被跳脫的直線（欄位分隔）剛好三條＝兩欄
      expect(r.replace(/\\\\/g, "").replace(/\\\|/g, "").split("|").length - 1).toBe(3);
    }
  });
});
