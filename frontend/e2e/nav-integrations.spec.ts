/**
 * 管理選單的「外部系統整合」子樹（使用者 2026-10-07）：預設展開、可以收合；子項目只寫產品名；
 * 已設定的整合名稱後面有圖示（seed_e2e 有 OPNsense 與 Proxmox VE）。
 */
import { test, expect } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

test("外部系統整合：預設展開、可收合；已設定的整合有圖示，沒設定的沒有", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });

  const menu = page.locator(".n-menu");
  await menu.getByText("管理", { exact: true }).click();
  const group = menu.getByText("外部系統整合", { exact: true });
  await expect(group).toBeVisible();
  const librenms = menu.getByText("LibreNMS", { exact: true });
  await expect(librenms).toBeVisible();
  await expect(menu.getByText(/^整合 /)).toHaveCount(0);

  await expect(page.getByTestId("nav-intg-on-firewall_admin")).toBeVisible();
  await expect(page.getByTestId("nav-intg-on-virt_admin")).toBeVisible();
  await expect(page.getByTestId("nav-intg-on-zabbix")).toHaveCount(0);

  await group.click();
  await expect(librenms).toBeHidden();
  await group.click();
  await expect(librenms).toBeVisible();
});
