import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * 範本裡用到的 naive-ui 元件（<n-xxx>）都要在同一個檔案匯入。
 *
 * naive-ui 沒有全域註冊：漏匯入時 Vue 把它當成不認識的標籤，預設插槽的內容直接印在畫面上、
 * 其他插槽整個不見，型別檢查與 build 都不會報錯。2026-10-07「取消計畫」就是這樣：
 * 確認框的文字出現在標題列，按鈕本身不見了。
 */
function vueFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return name === "node_modules" ? [] : vueFiles(p);
    return p.endsWith(".vue") ? [p] : [];
  });
}

describe("naive-ui 元件匯入", () => {
  it("每個 <n-xxx> 都有對應的 Nxxx 匯入", () => {
    const root = join(__dirname, "..", "..");
    const missing: string[] = [];
    let scanned = 0;
    for (const f of vueFiles(root)) {
      const src = readFileSync(f, "utf8");
      // 只看 <template> 區塊（照位置切，不用正規表示式去濾 HTML：CodeQL #50～#52）
      const start = src.indexOf("<template");
      const end = src.lastIndexOf("</template>");
      const template = start >= 0 && end > start ? src.slice(start, end) : "";
      if (template) scanned += 1;
      const tags = new Set([...template.matchAll(/<(n-[a-z0-9-]+)/g)].map((m) => m[1]));
      for (const tag of tags) {
        const name = "N" + tag.slice(2).split("-").map((p) => p[0].toUpperCase() + p.slice(1)).join("");
        if (!new RegExp(`\\b${name}\\b`).test(src)) missing.push(`${f.slice(root.length + 1)}: <${tag}>`);
      }
    }
    expect(scanned, "一個 <template> 都沒切到：檢查方式失效").toBeGreaterThan(50);
    expect(missing).toEqual([]);
  });
});
