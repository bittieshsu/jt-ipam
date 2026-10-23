/**
 * 儀表板「IP 使用率」改成最多 4 個子網路各自的使用率（客戶 2026-10-23）：
 * 沒選 → 自動顯示使用率最高的 4 個並標明「自動」；挑 2 個存檔 → 只剩那 2 個；改回自動 → 又是 4 個。
 * 需要至少 4 個有容量的子網路，其中兩個能用「192.168.」與「10.20.」搜尋到
 * （tests/seed_e2e.py 有 10.20.0.0/24；另外補一個 192.168. 開頭的就夠）。
 * 結束時把偏好還原（偏好跟著帳號，別影響其他 spec）。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function token(request: APIRequestContext): Promise<Record<string, string>> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

/** 只動 pinned 裡的 usage_subnets：PATCH pinned 會整包換掉，別的釘選要原樣帶回去 */
async function setUsagePins(request: APIRequestContext, H: Record<string, string>, ids: string[]): Promise<void> {
  const pref = await (await request.get("/api/v1/me/preferences", { headers: H })).json();
  await request.patch("/api/v1/me/preferences", {
    headers: H, data: { pinned: { ...(pref.pinned ?? {}), usage_subnets: ids } },
  });
}

async function pick(page: Page, term: RegExp | string, query: string): Promise<void> {
  await page.getByTestId("subnet-usage-select").click();
  await page.keyboard.type(query);
  const opt = page.locator(".n-base-select-option").filter({ hasText: term }).first();
  await expect(opt).toBeVisible({ timeout: 10_000 });
  await opt.click();
  await page.keyboard.press("Escape");
}

test("IP 使用率：自動顯示前 4 個 → 自選 2 個 → 改回自動", async ({ page, request }) => {
  const H = await token(request);
  const before = await (await request.get("/api/v1/me/preferences", { headers: H })).json();
  await setUsagePins(request, H, []);
  try {
    await page.goto("/login");
    await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
    await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
    await page.getByRole("button", { name: "登入", exact: true }).click();
    await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
    await page.goto("/");

    const card = page.getByTestId("subnet-usage-card");
    await expect(card.getByTestId("subnet-usage-auto")).toContainText("自動", { timeout: 15_000 });
    await expect(card.getByTestId("subnet-usage-gauge")).toHaveCount(4);

    // 挑兩個（伺服器端搜尋）
    await card.getByTestId("subnet-usage-edit").click();
    await pick(page, /^192\.168\./, "192.168.");
    await pick(page, /^10\.20\.0\.0\/24/, "10.20.");
    const saved = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith("/api/v1/me/preferences"));
    await page.getByTestId("subnet-usage-save").click();
    expect((await saved).postDataJSON().pinned.usage_subnets).toHaveLength(2);
    await expect(card.getByTestId("subnet-usage-gauge")).toHaveCount(2, { timeout: 15_000 });
    await expect(card.getByTestId("subnet-usage-auto")).toHaveCount(0);
    await expect(card.getByTestId("subnet-usage-gauge").nth(1)).toContainText("10.20.0.0/24");
    await expect(card.locator(".n-card-header")).toContainText("2 / 4");

    // 重新整理後還在（存在伺服器端的偏好）
    await page.reload();
    await expect(card.getByTestId("subnet-usage-gauge")).toHaveCount(2, { timeout: 15_000 });

    // 改回自動
    await card.getByTestId("subnet-usage-edit").click();
    await page.getByTestId("subnet-usage-reset").click();
    await page.getByTestId("subnet-usage-save").click();
    await expect(card.getByTestId("subnet-usage-gauge")).toHaveCount(4, { timeout: 15_000 });
    await expect(card.getByTestId("subnet-usage-auto")).toBeVisible();
  } finally {
    await setUsagePins(request, H, before.pinned?.usage_subnets ?? []);
  }
});
