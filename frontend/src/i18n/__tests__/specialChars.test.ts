/**
 * vue-i18n 的特殊字元：裸的 @ 是連結語法、| 是複數分隔。正式 build 裡寫錯不報錯，
 * 只是那一格畫不出來（2026-10-22 新增帳號的 Email 欄整個消失，就是 placeholder 裡的 name@example.com）。
 * 要顯示這兩個字元一律寫成 {'@'}、{'|'}。
 */
import { describe, expect, it } from "vitest";
import zh from "../zh-TW.json";
import en from "../en-US.json";
import ja from "../ja-JP.json";

function walk(o: unknown, path: string, out: string[]) {
  if (typeof o === "string") {
    const bare = o.replace(/\{'[@|]'\}/g, "");
    if (/[@|]/.test(bare)) out.push(`${path}: ${o}`);
  } else if (o && typeof o === "object") {
    for (const [k, v] of Object.entries(o)) walk(v, path ? `${path}.${k}` : k, out);
  }
}

describe("i18n 不可以有裸的 @ 與 |", () => {
  for (const [name, msgs] of [["zh-TW", zh], ["en-US", en], ["ja-JP", ja]] as const) {
    it(name, () => {
      const bad: string[] = [];
      walk(msgs, "", bad);
      expect(bad).toEqual([]);
    });
  }
});
