import { test, expect, type Page } from "@playwright/test";

/** 掃描代理頁：子網路數字可以點，彈出這台代理負責哪些子網路；點 CIDR 到子網路頁（使用者 2026-10-22）。 */
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

test("子網路數字可以點，列出負責的子網路", async ({ page }) => {
  await login(page);
  await page.goto("/scan-agents");
  const count = page.getByTestId("agent-subnet-count").first();
  await expect(count).toBeVisible({ timeout: 15_000 });
  const n = Number((await count.innerText()).trim());
  await count.click();
  const dlg = page.getByTestId("agent-subnet-list");
  await expect(dlg).toBeVisible();
  const rows = dlg.locator("tbody tr");
  await expect(rows).toHaveCount(n);
  await expect(rows.first()).toContainText(/\d+\.\d+\.\d+\.\d+\/\d+/);
  await rows.first().getByRole("button").first().click();
  await expect(page).toHaveURL(/\/subnets\/[0-9a-f-]{36}/);
});

test("掃描代理可以改名；說明刪光儲存後真的清空", async ({ page }) => {
  await login(page);
  await page.goto("/scan-agents");
  const row = page.locator(".n-data-table-tr").filter({ has: page.getByTestId("agent-subnet-count") }).first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  await row.getByRole("button", { name: /編輯/ }).click();
  const name = page.getByTestId("agent-name").locator("input");
  await expect(name).toBeEnabled();
  const orig = await name.inputValue();
  await name.fill(`${orig}-renamed`);
  const desc = page.locator(".n-modal textarea").first();
  await desc.fill("暫時的說明");
  await page.locator(".n-modal").getByRole("button", { name: /儲存/ }).click();
  await expect(page.locator(".n-data-table-tr", { hasText: `${orig}-renamed` })).toBeVisible();

  // 再編輯：清空說明、名稱改回原本
  await page.locator(".n-data-table-tr", { hasText: `${orig}-renamed` }).getByRole("button", { name: /編輯/ }).click();
  await page.getByTestId("agent-name").locator("input").fill(orig);
  await page.locator(".n-modal textarea").first().fill("");
  await page.locator(".n-modal").getByRole("button", { name: /儲存/ }).click();
  await expect(page.locator(".n-data-table-tr", { hasText: orig }).first()).toBeVisible();
  await page.locator(".n-data-table-tr").filter({ hasText: orig }).first().getByRole("button", { name: /編輯/ }).click();
  await expect(page.locator(".n-modal textarea").first()).toHaveValue("");
});
