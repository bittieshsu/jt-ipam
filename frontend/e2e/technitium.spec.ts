/**
 * Technitium DNS Server（DNS＋DHCP，使用者 2026-10-08）。對真的 Technitium 跑：
 *   docker run -d --name tdns-test --network <橋接網路> -e DNS_SERVER_ADMIN_PASSWORD=… technitium/dns-server
 *   建唯讀帳號與群組（DHCP、區域檢視權限，要同步的區域另外給群組檢視權限），用它建 API token；
 *   建一個啟用的 DHCP 範圍，用 busybox `udhcpc` 從同一個橋接網路要一筆真的租約（做法見 TEST_CHECKLIST 7b3a）
 *   E2E_TECHNITIUM_URL=http://172.31.250.2:5380 E2E_TECHNITIUM_TOKEN=…（後端的外連防護擋迴路位址：要用橋接網路的位址）
 * 沒給就跳過（其餘行為由後端測試涵蓋）。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const URL = process.env.E2E_TECHNITIUM_URL || "";
const TOKEN = process.env.E2E_TECHNITIUM_TOKEN || "";
test.skip(!ADMIN_PASS || !URL || !TOKEN, "需要 E2E_ADMIN_PASS、E2E_TECHNITIUM_URL、E2E_TECHNITIUM_TOKEN");

const TAG = `tdns-e2e-${Date.now().toString(36)}`;

async function token(request: APIRequestContext): Promise<string> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).access_token;
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

test("Technitium DHCP：新增、測試連線講出版本與權限、立即同步、範圍頁籤列出閘道與 DNS", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await login(page);
  await page.goto("/technitium-dhcp");
  await page.getByTestId("tdns-create").click();
  await page.getByTestId("tdns-name").locator("input").fill(TAG);
  await page.getByTestId("tdns-url").locator("input").fill(URL);
  // 沒填 token 存不了
  await page.getByTestId("tdns-save").click();
  await expect(page.locator(".n-message")).toContainText(/API token/);
  await page.getByTestId("tdns-token").locator("input").fill(TOKEN);
  await page.getByTestId("tdns-save").click();
  const row = page.getByTestId("tdns-list").locator("tr", { hasText: TAG });
  await expect(row).toBeVisible();

  await row.getByTestId("tdns-test").click();
  await expect(page.locator(".n-message").filter({ hasText: "連線成功" })).toContainText(/Technitium \d/, { timeout: 40_000 });

  await row.getByTestId("tdns-sync").click();
  await expect.poll(async () => {
    await page.getByRole("button", { name: /重新整理/ }).first().click();
    return row.innerText();
  }, { timeout: 60_000 }).toMatch(/個範圍/);

  await page.locator(".n-tabs-tab").filter({ hasText: "範圍" }).click();
  if (await page.locator(".n-tabs-pane-wrapper .n-select").count()) {
    await page.locator(".n-tabs-pane-wrapper .n-select").first().click();
    await page.locator(".n-base-select-option", { hasText: TAG }).click();
  }
  const scopes = page.getByTestId("tdns-scopes");
  await expect(scopes.locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });
  await expect(scopes).toContainText("啟用中");

  // 收尾：刪掉（也收回它寫進共用表的資料）
  const list = await (await request.get("/api/v1/technitium-dhcp/servers", { headers: auth })).json();
  const mine = list.items.find((x: { name: string }) => x.name === TAG);
  expect((await request.delete(`/api/v1/technitium-dhcp/servers/${mine.id}`, { headers: auth })).status()).toBe(204);
});

test("Technitium 當 DNS 伺服器：測試連線講出讀得到幾個區域", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  const created = await request.post("/api/v1/dns/servers", { headers: auth, data: {
    name: `${TAG}-dns`, type: "technitium", api_url: URL, api_key: TOKEN,
    extra_config: JSON.stringify({ verify_tls: false }), enabled: true } });
  expect(created.status()).toBe(201);
  const dns = await created.json();
  try {
    await login(page);
    await page.goto("/dns");
    const row = page.locator("tr", { hasText: `${TAG}-dns` });
    await expect(row).toBeVisible({ timeout: 15_000 });
    await row.getByRole("button", { name: /測試/ }).click();
    await expect(page.locator(".n-message").filter({ hasText: "連線成功" })).toContainText(/讀得到 \d+ 個區域/,
                                                                                          { timeout: 40_000 });
  } finally {
    await request.delete(`/api/v1/dns/servers/${dns.id}`, { headers: auth });
  }
});
