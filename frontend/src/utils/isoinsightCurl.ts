/**
 * ISOinsight 測試連線的 curl 指令：照來源設定送一次一模一樣的登入與讀租約，讓使用者在 jt-ipam 主機上自己測
 * （使用者 2026-10-14）。設備回「帳密錯誤」時，用它分辨是帳密本身、還是我們送的格式有問題。
 * 密碼一律是佔位字，不放真的密碼（也拿不到：前端只知道有沒有設密碼）。
 */
export interface IsoCurlSource {
  base_url: string; login_path: string; login_method: "GET" | "POST"; post_format: "form" | "json";
  username: string; username_param: string; password_param: string; auth_mode: "cookie" | "token";
  token_header: string; token_prefix: string; token_in: "header" | "query"; token_param: string;
  lease_path: string; verify_tls: boolean;
}

const COOKIE_JAR = "/tmp/isoinsight.cookies";

/** 單引號包起來給 shell；內容裡的單引號改寫成 '\'' */
export function shq(s: string): string {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}

function url(base: string, path: string): string {
  return `${base.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
}

export function isoCurlCommands(src: IsoCurlSource, ph: { password: string; token: string }): { login: string; fetch: string } {
  const base = `curl -sS${src.verify_tls ? "" : " -k"}`;
  const cookie = src.auth_mode === "cookie";
  const loginUrl = shq(url(src.base_url, src.login_path));
  const leaseUrl = shq(url(src.base_url, src.lease_path));
  const enc = (k: string, v: string) => `--data-urlencode ${shq(`${k}=${v}`)}`;
  const lines: string[] = [];
  if (src.login_method === "GET") {
    lines.push(`${base} -G ${loginUrl}`, enc(src.username_param, src.username), enc(src.password_param, ph.password));
  } else if (src.post_format === "json") {
    lines.push(`${base} -X POST ${loginUrl}`, `-H ${shq("Content-Type: application/json;charset=UTF-8")}`,
               `-d ${shq(JSON.stringify({ [src.username_param]: src.username, [src.password_param]: ph.password }))}`);
  } else {
    lines.push(`${base} -X POST ${loginUrl}`, enc(src.username_param, src.username), enc(src.password_param, ph.password));
  }
  lines.push(`-H ${shq("Accept: application/json")}`);
  if (cookie) lines.push(`-c ${COOKIE_JAR}`);

  let fetch: string[];
  if (cookie) fetch = [`${base} ${leaseUrl}`, `-b ${COOKIE_JAR}`];
  else if (src.token_in === "query") fetch = [`${base} -G ${leaseUrl}`, enc(src.token_param, ph.token)];
  else {
    const prefix = (src.token_prefix || "").trim();
    fetch = [`${base} ${leaseUrl}`, `-H ${shq(`${src.token_header}: ${prefix ? `${prefix} ` : ""}${ph.token}`)}`];
  }
  return { login: lines.join(" \\\n  "), fetch: fetch.join(" \\\n  ") };
}
