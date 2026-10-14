/**
 * ISOinsight 測試連線的 curl 指令（使用者 2026-10-14：「測試連線那邊也要提供 curl 指令給 user 自己 CLI 測」）。
 * 客戶重填帳密仍然被拒時，要能在 jt-ipam 主機上自己送一次一模一樣的登入，分辨是帳密還是我們送的格式。
 * 指令跟著來源設定走（方法、格式、參數名、Token 放哪、TLS），密碼一律是佔位字、不放真的密碼。
 */
import { describe, expect, it } from "vitest";
import { isoCurlCommands, shq } from "../isoinsightCurl";

const PH = { password: "<密碼>", token: "<SessionID>" };
const VENDOR = {
  base_url: "https://192.0.2.10/", login_path: "/logon", login_method: "POST" as const, post_format: "json" as const,
  username: "ipam-ro", username_param: "username", password_param: "password", auth_mode: "token" as const,
  token_header: "Authorization", token_prefix: "Bearer", token_in: "query" as const, token_param: "sessionid",
  lease_path: "/isosvc?act=DhcpLease", verify_tls: false,
};

describe("isoCurlCommands", () => {
  it("原廠的方式：POST JSON 到 /logon，SessionID 放網址參數", () => {
    const c = isoCurlCommands(VENDOR, PH);
    expect(c.login).toContain("curl -sS -k -X POST 'https://192.0.2.10/logon'");
    expect(c.login).toContain("-H 'Content-Type: application/json;charset=UTF-8'");
    expect(c.login).toContain(`-d '{"username":"ipam-ro","password":"<密碼>"}'`);
    expect(c.fetch).toContain("curl -sS -k -G 'https://192.0.2.10/isosvc?act=DhcpLease'");
    expect(c.fetch).toContain("--data-urlencode 'sessionid=<SessionID>'");
  });

  it("驗證 TLS 時不加 -k；Token 放標頭時帶前綴", () => {
    const c = isoCurlCommands({ ...VENDOR, verify_tls: true, token_in: "header" }, PH);
    expect(c.login).not.toContain(" -k ");
    expect(c.fetch).toContain("-H 'Authorization: Bearer <SessionID>'");
  });

  it("form 與 GET 用 --data-urlencode（參數名照設定）", () => {
    const form = isoCurlCommands({ ...VENDOR, post_format: "form", username_param: "user", password_param: "pass" }, PH);
    expect(form.login).toContain("--data-urlencode 'user=ipam-ro'");
    expect(form.login).toContain("--data-urlencode 'pass=<密碼>'");
    expect(form.login).not.toContain("Content-Type: application/json");
    const get = isoCurlCommands({ ...VENDOR, login_method: "GET" }, PH);
    expect(get.login).toContain("curl -sS -k -G 'https://192.0.2.10/logon'");
  });

  it("Cookie 模式：登入存 Cookie、讀租約帶 Cookie", () => {
    const c = isoCurlCommands({ ...VENDOR, auth_mode: "cookie" }, PH);
    expect(c.login).toContain("-c /tmp/isoinsight.cookies");
    expect(c.fetch).toContain("-b /tmp/isoinsight.cookies");
    expect(c.fetch).not.toContain("sessionid");
  });

  it("帳號裡的單引號要跳脫，指令不會斷掉", () => {
    expect(shq("o'brien")).toBe(`'o'\\''brien'`);
    const c = isoCurlCommands({ ...VENDOR, post_format: "form", username: "o'brien" }, PH);
    expect(c.login).toContain(`--data-urlencode 'username=o'\\''brien'`);
  });
});
