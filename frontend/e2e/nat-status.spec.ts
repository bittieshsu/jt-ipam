import { test, expect, type Page } from "@playwright/test";

/**
 * NAT 規則的啟用／停用（使用者 2026-10-11：有的防火牆可以停用規則，例如 OPNsense；沒啟用的那一列要看得出來）。
 * 樣本（tests/seed_e2e.py）：e2e-https-forward 啟用、e2e-off-forward 停用。
 *   1. 狀態欄與整列變淡
 *   2. 狀態篩選
 *   3. 表單的「啟用」開關；並排欄位那幾排與下一個欄位標題之間有間距（以前「篩選規則關聯」貼著上一排）
 */
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(process.env.E2E_ADMIN_USER || "admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

test("停用的規則：狀態欄寫「已停用」、整列變淡；狀態篩選", async ({ page }) => {
  await login(page);
  await page.goto("/nat");
  const off = page.locator(".n-data-table-tbody tr", { hasText: "e2e-off-forward" });
  const on = page.locator(".n-data-table-tbody tr", { hasText: "e2e-https-forward" });
  await expect(off).toBeVisible({ timeout: 15_000 });
  await expect(off).toContainText("已停用");
  await expect(off).toHaveClass(/jt-nat-row-disabled/);
  const opacity = await off.locator("td", { hasText: "e2e-off-forward" }).evaluate((el) => getComputedStyle(el).opacity);
  expect(Number(opacity)).toBeLessThan(0.8);
  await expect(on).toContainText("啟用");
  await expect(on).not.toHaveClass(/jt-nat-row-disabled/);

  await page.getByTestId("nat-status-filter").click();
  await page.locator(".n-base-select-option", { hasText: "已停用" }).click();
  await expect(off).toBeVisible();
  await expect(on).toHaveCount(0);
  await page.getByTestId("nat-status-filter").click();
  await page.locator(".n-base-select-option", { hasText: /^啟用$/ }).click();
  await expect(on).toBeVisible();
  await expect(off).toHaveCount(0);
});

test("表單的「啟用」開關；並排欄位下方有間距", async ({ page }) => {
  await login(page);
  await page.goto("/nat");
  await page.locator(".n-data-table-tbody tr", { hasText: "e2e-off-forward" }).locator("td").nth(1).click();
  const modal = page.locator(".n-modal");
  const sw = modal.getByTestId("nat-enabled");
  await expect(sw).toBeVisible();
  await expect(sw).not.toHaveClass(/n-switch--active/);   // 停用的規則：開關是關的
  await modal.getByRole("button", { name: "關閉" }).click();
  await page.locator(".n-data-table-tbody tr", { hasText: "e2e-https-forward" }).locator("td").nth(1).click();
  await expect(modal.getByTestId("nat-enabled")).toHaveClass(/n-switch--active/);

  // 「NAT 反射」那一排與下面「篩選規則關聯」的標題之間要有間距。
  // 用 poll 量到穩定為止：對話框開啟動畫中是縮放的，量到的距離只有一半（18px 量成 9px）
  const row = modal.locator(".jt-nat-row").last();
  const nextLabel = modal.locator(".n-form-item-label", { hasText: "篩選規則關聯" });
  await expect.poll(async () => row.evaluate((r, label) =>
    (label as HTMLElement).getBoundingClientRect().top - r.getBoundingClientRect().bottom,
  await nextLabel.elementHandle()), { message: "上一排與下一個欄位標題之間的距離" }).toBeGreaterThanOrEqual(12);
});
