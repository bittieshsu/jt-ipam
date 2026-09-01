/**
 * 六個主控台的「已存帳密」下拉都要能選「用別組帳密」。
 *
 * 使用者回報（2026-08-31）：「我只能選存過的，沒辦法建新的認證。」
 * 原本清空的方式是下拉右邊那個**只有 hover 才出現的 ✕** —— 打開下拉只看到已存的那一筆，
 * 自然會以為沒有別的路。可發現性不是「做得到」就算數。
 *
 * 六個主控台是同一種互動，只改一個等於留下五個不一致的地方。
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const CONSOLES = [
  "SshTerminal.vue", "SftpBrowser.vue", "RdpScreen.vue",
  "VncScreen.vue", "NoVncScreen.vue", "BmcScreen.vue",
];

describe.each(CONSOLES)("%s 的帳密下拉", (file) => {
  const src = readFileSync(join(root, file), "utf-8");

  it("下拉裡要有「使用其他帳密」這個選項", () => {
    expect(src.includes("ssh.cred_manual"),
      "只剩 hover 才看得到的 ✕ 可以清空 —— 使用者找不到，就等於做不到").toBe(true);
  });

  it("那個選項的值要是 null（＝回到手動輸入）", () => {
    const i = src.indexOf("ssh.cred_manual");
    expect(i).toBeGreaterThan(-1);
    expect(src.slice(i, i + 120)).toContain("null");
  });
});
