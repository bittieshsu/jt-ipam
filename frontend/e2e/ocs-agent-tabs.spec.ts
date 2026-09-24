/**
 * OCS 整合頁要比照 Wazuh：「代理數」與「未裝 Agent 的 IP」兩個頁籤。
 * 代理數要一台電腦一筆 —— seed_e2e 的 web-01 與 198.51.100.7 是同一台 OCS 電腦（id 101）。
 */
import { test, expect } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

test("OCS：代理數一台電腦一筆、未裝 Agent 的 IP 不含已盤點的", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/ocs");

  const agentsTab = page.locator(".n-tabs-tab", { hasText: /代理數/ });
  await expect(agentsTab).toHaveText(/代理數 \(1\)/, { timeout: 20_000 });
  const missingTab = page.locator(".n-tabs-tab", { hasText: /未裝 Agent 的 IP/ });
  await expect(missingTab).toBeVisible();

  await agentsTab.click();
  const row = page.locator(".n-data-table-tr", { hasText: "101" }).first();
  await expect(row).toContainText("10.20.0.10");
  await expect(row).toContainText("198.51.100.7");
  await expect(row).toContainText("E2E");

  await missingTab.click();
  const missTable = page.locator(".n-tab-pane:visible .n-data-table").first();
  await expect(missTable).toContainText("10.20.0.11");            // app-01：有名字、沒盤點
  await expect(missTable).not.toContainText("10.20.0.10");        // web-01 已盤點
});
