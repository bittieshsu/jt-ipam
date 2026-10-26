/** 不存在的網址顯示「找不到頁面」，不是整頁空白（2026-10-25 實測：打錯成 /ip-requests 整頁空白，看起來像當掉）。 */
import { test, expect } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

test("打錯的網址：顯示找不到頁面、選單照常、可回儀表板", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await page.goto("/ip-requests");
  await expect(page.getByTestId("not-found")).toContainText("找不到這個頁面");
  await expect(page.getByTestId("not-found")).toContainText("/ip-requests");
  await expect(page.getByText("IP 申請", { exact: true }).first()).toBeVisible();   // 選單還在
  await page.getByRole("button", { name: "回儀表板" }).click();
  await expect(page).toHaveURL(/\/$/);
});
