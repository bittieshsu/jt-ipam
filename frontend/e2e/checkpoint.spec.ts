/**
 * Check Point（R81.20，Management API 唯讀，使用者 2026-10-08）。兩種跑法：
 *   - 模擬伺服器：`backend/tests/checkpoint_mock.py` 的 CheckPointMock 起在本機，被測後端要設
 *     OUTBOUND_ALLOW_CIDRS=127.0.0.1/32；E2E_CP_URL=http://127.0.0.1:<port> E2E_CP_KEY=cp-key-0123456789abcdef
 *   - 實機：E2E_CP_URL=https://<管理伺服器> E2E_CP_KEY=<Read Only All 管理員的 API key>（自簽憑證設 E2E_CP_INSECURE=1）
 * 沒給就跳過（其餘行為由後端測試涵蓋）。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const URL = process.env.E2E_CP_URL || "";
const KEY = process.env.E2E_CP_KEY || "";
const INSECURE = process.env.E2E_CP_INSECURE === "1";
test.skip(!ADMIN_PASS || !URL || !KEY, "需要 E2E_ADMIN_PASS、E2E_CP_URL、E2E_CP_KEY");

const TAG = `cp-e2e-${Date.now().toString(36)}`;

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

test("Check Point：新增、測試連線講出 API 版本、立即同步、規則／物件／閘道頁籤有資料、搜尋走後端", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await login(page);
  await page.goto("/checkpoint");
  await page.getByTestId("cp-create").click();
  await page.getByTestId("cp-name").locator("input").fill(TAG);
  await page.getByTestId("cp-url").locator("input").fill(URL);
  // 沒填 API key 存不了
  await page.getByTestId("cp-save").click();
  await expect(page.locator(".n-message")).toContainText(/API key/);
  await page.getByTestId("cp-secret").locator("input").fill(KEY);
  if (INSECURE) await page.locator(".n-modal .n-form-item", { hasText: /驗證 TLS/ }).locator(".n-switch").click();
  await page.getByTestId("cp-save").click();
  const row = page.getByTestId("cp-list").locator("tr", { hasText: TAG });
  await expect(row).toBeVisible();

  await row.getByTestId("cp-test").click();
  await expect(page.locator(".n-message").filter({ hasText: /API \d/ })).toBeVisible({ timeout: 60_000 });

  await row.getByTestId("cp-sync").click();
  await expect.poll(async () => {
    await page.getByRole("button", { name: /重新整理/ }).first().click();
    return row.innerText();
  }, { timeout: 120_000 }).toMatch(/規則 \d+/);

  const pick = async () => {
    const sel = page.locator(".n-tabs-pane-wrapper .n-select").first();
    if (await sel.count() && await sel.isVisible()) {
      await sel.click();
      await page.locator(".n-base-select-option", { hasText: TAG }).click();
    }
  };
  await page.locator(".n-tabs-tab").filter({ hasText: "存取規則" }).click();
  await pick();
  const rules = page.getByTestId("cp-rules");
  await expect(rules.locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });
  const total = await rules.locator("tbody tr").count();
  // 搜尋一個一定搜不到的字：清單要變空（證明是後端搜尋，不是只篩目前這一頁）
  await page.getByTestId("cp-search-rules").locator("input").fill("zz-no-such-rule-zz");
  await expect(rules.locator("tbody tr.n-data-table-tr")).toHaveCount(0, { timeout: 10_000 });
  await page.getByTestId("cp-search-rules").locator("input").fill("");
  await expect.poll(() => rules.locator("tbody tr.n-data-table-tr").count(), { timeout: 10_000 }).toBe(total);

  await page.locator(".n-tabs-tab").filter({ hasText: "網路物件" }).click();
  await expect(page.getByTestId("cp-objects").locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });
  await page.locator(".n-tabs-tab").filter({ hasText: "閘道" }).click();
  await expect(page.getByTestId("cp-gateways").locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });

  // 收尾：刪掉（也收回它寫進共用 NAT 表的資料）
  const list = await (await request.get("/api/v1/checkpoint/servers", { headers: auth })).json();
  const mine = list.items.find((x: { name: string }) => x.name === TAG);
  expect((await request.delete(`/api/v1/checkpoint/servers/${mine.id}`, { headers: auth })).status()).toBe(204);
});
