/**
 * IP 詳細頁的「所屬別名」要跟上下兩段同一種排法。
 *
 * 以前是一行「所屬別名：[名稱（廠牌）]」夾在規則清單與「any 規則不列出」的註記中間：
 * 標題是深色內文字、跟標籤擠同一行，廠牌塞在括號裡，看不出是哪一台防火牆。
 * 樣本：seed_e2e 的 fw-e2e／web_hosts 涵蓋 198.51.100.7。
 */
import { test, expect } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

test("所屬別名：獨立標題、每筆寫出防火牆，註記緊跟規則", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/addresses?q=198.51.100.7");
  await page.getByText("198.51.100.7", { exact: true }).first().click();
  await page.waitForURL(/addresses\/[0-9a-f-]{36}/);

  const title = page.getByText(/^\s*所屬別名（1）\s*$/);
  await expect(title).toBeVisible({ timeout: 20_000 });
  const row = page.locator("div", { hasText: /fw-e2e｜web_hosts/ }).last();
  await expect(row).toContainText("opnsense");
  await expect(row).toContainText("e2e：對外網站主機");      // 別名說明以前沒顯示

  const t = (await title.boundingBox())!;
  const r = (await row.boundingBox())!;
  expect(r.y, "別名那一行要在標題下面，不是跟標題擠同一行").toBeGreaterThanOrEqual(t.y + t.height - 1);
  const note = (await page.getByText(/來源或目的為 any 的規則/).boundingBox())!;
  expect(note.y, "any 註記在說明規則，要在別名區塊上面").toBeLessThan(t.y);
});
