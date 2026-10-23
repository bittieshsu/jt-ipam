/**
 * 編輯表單的「說明」要能清空（使用者 10-22：掃描代理的說明刪光按儲存，進來又出現了）。
 * 原因是 `description: x.description || undefined`：空字串變 undefined，JSON 裡就沒這個欄位，後端當作沒改。
 * 一次改掉了 26 處；這裡守住不要再出現。要清空就送空字串（`?? ""`）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

function vueFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? vueFiles(join(dir, e.name)) : e.name.endsWith(".vue") ? [join(dir, e.name)] : []);
}

describe("說明欄位可以清空", () => {
  it("沒有 `description: x.description || undefined`", () => {
    const root = resolve(__dirname, "..");
    const bad = vueFiles(root).filter((f) => /description:\s*[\w.]+\.description\s*\|\|\s*undefined/.test(readFileSync(f, "utf8")));
    expect(bad.map((f) => f.slice(root.length + 1))).toEqual([]);
  });
});
