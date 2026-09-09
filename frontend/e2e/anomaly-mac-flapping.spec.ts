import { test, expect } from "@playwright/test";

/**
 * 異常偵測：一個 IP 頻繁更換 MAC，而且可以逐 IP 忽略。
 *
 * 為什麼忽略機制是這條規則的一部分：Windows 11 / macOS / iOS / Android 開了隱私
 * 隨機化之後每次連線都換 MAC。沒有忽略，裝置一多就會把整頁洗掉，使用者只能把整條
 * 規則關掉 —— 連真正的 IP 搶用、DHCP 池異常也一起看不到。
 */
const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";

test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS env 才能跑");

test("頻繁換 MAC 的 IP 會被列出，而且可以忽略", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });

  // 直接用 ?tab= 進到那一類（通知點進來也是走這條路）
  await page.goto("/anomaly?tab=mac_flapping");
  await page.getByRole("button", { name: /執行偵測/ }).click();
  await expect(page.locator(".n-data-table-tbody .n-data-table-tr").first())
    .toBeVisible({ timeout: 60_000 });

  const body = await page.locator("body").innerText();
  test.skip(!body.includes("10.20.0.10"), "這個環境沒有頻繁換 MAC 的樣本");

  // 要看得出「這些是隨機化位址」—— 沒有這個資訊，人無從判斷該不該忽略。
  // 而且布林值要印成「是／否」，不是裸的 true（第一版就是這樣露出來的）
  await expect(page.locator(".n-data-table-thead")).toContainText("MAC 數");
  const row = page.locator(".n-data-table-tr", { hasText: "10.20.0.10" }).first();
  await expect(row).toBeVisible();
  await expect(row).not.toContainText("true");
  // 內部 UUID 不該預設顯示（可在「欄位」勾選）
  await expect(page.locator(".n-data-table-thead")).not.toContainText("IP 內部編號");

  // 版面：MAC 欄不折行，很容易蓋到「操作」欄的按鈕上（第一版就是這樣）
  const macCell = row.locator("td").nth(4);
  const btn = row.getByRole("button", { name: "忽略這個 IP" });
  const [mb, bb] = [await macCell.boundingBox(), await btn.boundingBox()];
  expect(mb!.x + mb!.width, "MAC 欄壓到操作欄的按鈕上").toBeLessThanOrEqual(bb!.x + 1);

  // 忽略之後那一列就不該再出現
  await row.getByRole("button", { name: "忽略這個 IP" }).click();
  await expect(page.locator(".n-data-table-tr", { hasText: "10.20.0.10" }))
    .toHaveCount(0, { timeout: 60_000 });
});
