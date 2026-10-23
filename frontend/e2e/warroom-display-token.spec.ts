/**
 * 電視顯示權杖：電視每天關機，隔天開機時瀏覽器還原的是已經清掉權杖的網址（10-23 使用者問到）。
 * 權杖要記在 localStorage（瀏覽器重開仍在），不是 sessionStorage（跟著瀏覽器關閉被清空）。
 * Playwright 的 storageState 只帶 localStorage 與 cookie、不帶 sessionStorage —— 正好就是「瀏覽器重開」。
 * 會改戰情室的全站設定（開關電視權杖），列在 global-state-specs.txt 依序跑。
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function headers(request: APIRequestContext): Promise<Record<string, string>> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

test("用權杖網址開一次，瀏覽器關掉重開（沒有權杖的網址）仍然直接進戰情室", async ({ browser, request }) => {
  const H = await headers(request);
  const me = await (await request.get("/api/v1/auth/me", { headers: H })).json();
  const before = await (await request.get("/api/v1/system/warroom", { headers: H })).json();
  const put = await request.put("/api/v1/system/warroom", { headers: H,
    data: { display_enabled: true, principal_user_id: me.id } });
  expect(put.status(), await put.text()).toBe(200);
  const { token } = await (await request.post("/api/v1/system/warroom/token", { headers: H, data: { days: 1 } })).json();
  try {
    // 第一天：用完整網址開
    const day1 = await browser.newContext();
    const p1 = await day1.newPage();
    await p1.goto(`/warroom?display=${encodeURIComponent(token)}`);
    await expect(p1.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
    await expect(p1).not.toHaveURL(/display=/);                    // 網址列不留權杖
    const state = await day1.storageState();
    await day1.close();
    // 隔天：瀏覽器重開，還原的是沒有權杖的網址
    const day2 = await browser.newContext({ storageState: state });
    const p2 = await day2.newPage();
    await p2.goto("/warroom");
    await expect(p2.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
    await expect(p2).not.toHaveURL(/\/login/);
    await day2.close();
  } finally {
    await request.delete("/api/v1/system/warroom/token", { headers: H });
    await request.put("/api/v1/system/warroom", { headers: H,
      data: { display_enabled: !!before.display_enabled, principal_user_id: before.principal_user_id ?? null } });
  }
});
