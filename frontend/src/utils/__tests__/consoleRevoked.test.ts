/**
 * 開著的主控台被收回權限時（帳號停用、強制登出、收回連線權限），後端以 4403 關閉
 * WebSocket（backend/app/core/console_guard.py）。以前畫面只說「已中斷」，使用者
 * 以為是網路問題、一直重連。每個主控台都要把原因講出來。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { CONSOLE_REVOKED, revokedCloseText } from "@/utils/wsError";
import zhTW from "@/i18n/zh-TW.json";
import enUS from "@/i18n/en-US.json";
import jaJP from "@/i18n/ja-JP.json";

const REASONS = ["account_inactive", "session_revoked", "target_removed", "permission_revoked"];

describe("主控台被收回權限", () => {
  it("4403 有原因、其他關閉代碼不管", () => {
    expect(CONSOLE_REVOKED).toBe(4403);
    expect(revokedCloseText({ code: 1000, reason: "" })).toBeNull();
    expect(revokedCloseText(undefined)).toBeNull();
    const txt = revokedCloseText({ code: 4403, reason: "access revoked: account_inactive" });
    expect(txt).toBeTruthy();
    expect(txt).not.toContain("account_inactive");     // 翻成句子，不是把代碼丟給使用者
    expect(revokedCloseText({ code: 4403, reason: "access revoked: something_new" })).toBeTruthy();
  });

  it("每個原因三語系都有翻譯", () => {
    for (const msgs of [zhTW, enUS, jaJP] as any[]) {
      expect(msgs.errors.console_revoked).toBeTruthy();
      for (const r of REASONS) expect(msgs.errors[`console_revoked_${r}`], r).toBeTruthy();
    }
  });

  it("每個主控台元件的關閉處理都有用到", () => {
    const files = ["SshTerminal", "SftpBrowser", "RdpScreen", "VncScreen", "NoVncScreen", "BmcScreen", "GuacView"]
      .map((n) => `src/components/${n}.vue`).concat(["src/rdweb/session.ts"]);
    for (const f of files) {
      const src = readFileSync(resolve(__dirname, "../../..", f), "utf-8");
      expect(src, f).toContain("revokedCloseText(");
    }
  });
});
