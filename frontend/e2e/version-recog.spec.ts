/**
 * 版本資訊頁的「選用資料庫」：Recog 指紋庫（探測用）。
 *
 * - 看得到裝了哪一版、幾條指紋、上次檢查時間；沒裝時講「未安裝」
 * - 「立即檢查更新」：回應用路由攔截（e2e 環境不一定連得到 GitHub），驗畫面會跟著更新
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

const STATUS = {
  installed: true, release: "3.2.0", databases: 51, fingerprints: 4671, skipped: 5,
  updated_at: "2026-09-29T07:00:00+00:00", checked_at: "2026-09-29T07:30:00+00:00",
  last_ok_at: "2026-09-29T07:30:00+00:00", latest: "3.2.0", error: null,
  project_url: "https://github.com/rapid7/recog", license: "BSD-2-Clause",
};

test("選用資料庫卡片：版本、筆數、授權；立即檢查更新後畫面跟著變", async ({ page }) => {
  await login(page);
  await page.goto("/version");
  const card = page.getByTestId("version-recog");
  await expect(card).toBeVisible({ timeout: 20_000 });
  // 真的後端：裝了就有版本號，沒裝就寫未安裝 —— 兩種都要講清楚，不可以空白
  await expect(card).toContainText(/Recog\s+(\d+\.\d+|未安裝)/);
  await expect(card).toContainText("BSD-2-Clause");

  await page.route("**/api/v1/system/recog/update", (route) => route.fulfill({ json: {
    result: { status: "updated", previous: "3.1.30", release: "3.2.0", latest: "3.2.0", fingerprints: 4671 },
    status: STATUS,
  } }));
  await page.getByTestId("version-recog-update").click();
  await expect(page.locator(".n-message").filter({ hasText: "已更新到 Recog 3.2.0" })).toBeVisible();
  await expect(card).toContainText("4,671 條指紋");
  await expect(card).toContainText("GitHub 最新版 3.2.0");
});

test("檢查失敗：錯誤原因要顯示在卡片上", async ({ page }) => {
  await login(page);
  await page.goto("/version");
  const card = page.getByTestId("version-recog");
  await expect(card).toBeVisible({ timeout: 20_000 });
  await page.route("**/api/v1/system/recog/update", (route) => route.fulfill({ json: {
    result: { status: "error", release: "3.2.0", error: "cannot reach GitHub (e2e)" },
    status: { ...STATUS, error: "cannot reach GitHub (e2e)" },
  } }));
  await page.getByTestId("version-recog-update").click();
  await expect(page.getByTestId("version-recog-error")).toContainText("cannot reach GitHub (e2e)");
});
