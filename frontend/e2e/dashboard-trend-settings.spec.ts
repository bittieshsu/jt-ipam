/**
 * 儀表板「近 14 日 稽核 / IP 異動 趨勢」的個人設定（使用者 2026-10-18）：排除子網路、排除 DHCP 範圍、只計入部分異動類型。
 * 存檔真的 PATCH 偏好、重新載入後圖例旁標出「已排除…」；最後把設定還原（偏好跟著帳號，別影響其他 spec）。
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function token(request: APIRequestContext): Promise<Record<string, string>> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

test("設定排除 DHCP 範圍與異動類型 → 圖例旁標出已排除", async ({ page, request }) => {
  const H = await token(request);
  try {
    await page.goto("/login");
    await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
    await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
    await page.getByRole("button", { name: "登入", exact: true }).click();
    await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
    await page.goto("/");
    await expect(page.getByTestId("trend-filter-tag")).toHaveCount(0, { timeout: 15_000 });
    await page.getByTestId("trend-settings-open").click();
    await expect(page.getByTestId("trend-settings")).toBeVisible();
    await page.getByTestId("trend-exclude-dhcp").click();
    await page.getByTestId("trend-event-groups").getByText("上線／離線").click();
    const saved = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith("/api/v1/me/preferences"));
    await page.getByTestId("trend-save").click();
    const body = (await saved).postDataJSON();
    expect(body.dashboard.trend.exclude_dhcp).toBe(true);
    expect(body.dashboard.trend.event_groups).not.toContain("status");
    await expect(page.getByTestId("trend-filter-tag")).toContainText("DHCP 範圍", { timeout: 15_000 });
    await expect(page.getByTestId("trend-filter-tag")).toContainText("只計入 5 類");
  } finally {
    await request.patch("/api/v1/me/preferences", { headers: H, data: { dashboard: null } });
  }
});
