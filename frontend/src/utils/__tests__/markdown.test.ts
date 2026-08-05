import { describe, expect, it } from "vitest";

import { renderMarkdown } from "../markdown";

describe("renderMarkdown", () => {
  // 客戶回報：AI 對話裡的工具名稱 `recent_ip_changes` 被渲染成 recent<em>ip</em>changes，
  // 底線之間的字變成斜體。CommonMark 明訂**字中間的底線不算強調**，就是為了 snake_case
  // 這種識別字；星號才允許字中強調。
  it("字中間的底線不是斜體（工具名稱要原樣顯示）", () => {
    const html = renderMarkdown("呼叫 recent_ip_changes 看看");
    expect(html).not.toContain("<em>");
    expect(html).toContain("recent_ip_changes");
  });

  it("多個底線的識別字也一樣", () => {
    const html = renderMarkdown("list_ai_findings 與 detect_external_exposure");
    expect(html).not.toContain("<em>");
    expect(html).toContain("list_ai_findings");
    expect(html).toContain("detect_external_exposure");
  });

  it("真正的底線斜體還是要能用", () => {
    expect(renderMarkdown("這是 _重點_ 沒錯")).toContain("<em>重點</em>");
  });

  it("星號斜體不受影響", () => {
    expect(renderMarkdown("這是 *重點* 沒錯")).toContain("<em>重點</em>");
  });

  it("粗體不受影響", () => {
    expect(renderMarkdown("這是 **重點**")).toContain("<strong>重點</strong>");
  });

  it("行內程式碼裡的底線原樣保留", () => {
    const html = renderMarkdown("用 `recent_ip_changes` 這支");
    expect(html).toContain("<code>recent_ip_changes</code>");
    expect(html).not.toContain("<em>");
  });

  it("仍然會跳脫 HTML（不能因為改規則而開了注入）", () => {
    expect(renderMarkdown("<img src=x onerror=alert(1)>")).not.toContain("<img");
  });
});
