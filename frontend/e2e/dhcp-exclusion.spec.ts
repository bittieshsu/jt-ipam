/**
 * DHCP 排除區間（0207，客戶 2026-10-12）：Windows DHCP 把設了固定 IP 的設備逐一排除在發放範圍外，
 * 以前 jt-ipam 不讀，那些位址被標成「DHCP 範圍」，還提示「建議把它排除在發放範圍外」。
 * 樣本（seed_e2e）：dc-dhcp-e2e 的範圍 10.20.0.80–99 挖掉 .85（static-printer-01）。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

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

async function idOf(request: APIRequestContext, auth: Record<string, string>, ip: string): Promise<string> {
  const found = await (await request.get(`/api/v1/addresses?q=${ip}&page_size=5`, { headers: auth })).json();
  const id = found.items?.find((x: { ip: string }) => String(x.ip).split("/")[0] === ip)?.id ?? "";
  expect(id, `找不到樣本 ${ip}（先跑 seed_e2e）`).not.toBe("");
  return id;
}

test("排除的位址：清單有圖示、詳細資料標「DHCP 排除」而不是「DHCP 範圍」", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  const excluded = await idOf(request, auth, "10.20.0.85");
  const inPool = await idOf(request, auth, "10.20.0.86");
  await login(page);

  await page.goto("/addresses?q=10.20.0.85");
  const icon = page.getByTestId("role-dhcp-excluded").first();
  await expect(icon).toBeVisible({ timeout: 20_000 });
  await icon.hover();
  await expect(page.locator(".n-tooltip").filter({ hasText: "DHCP 排除" })).toContainText("dc-dhcp-e2e：10.20.0.85");

  await page.goto(`/addresses/${excluded}`);
  const tags = page.locator(".n-tag");
  await expect(tags.filter({ hasText: /^DHCP 排除$/ }).first()).toBeVisible({ timeout: 20_000 });
  await expect(tags.filter({ hasText: /^DHCP 範圍$/ })).toHaveCount(0);
  await expect(page.getByText("建議把它排除在發放範圍外")).toHaveCount(0);

  await page.goto(`/addresses/${inPool}`);
  await expect(tags.filter({ hasText: /^DHCP 範圍$/ }).first()).toBeVisible({ timeout: 20_000 });
  await expect(tags.filter({ hasText: /^DHCP 排除$/ })).toHaveCount(0);
});
