import { test, expect } from "@playwright/test";

/**
 * 上線判定的證據勾選：分組＋對齊。
 *
 * 使用者回報「這裡這樣排 很不整齊」—— 原本是自由換行，每個項目字數不同，
 * 每一列的起點就跟著跑。改成「種類在列首 + 固定欄寬的網格」。
 * 版面問題要**量幾何**，截圖看起來還好不算數。
 */
const PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!PASS, "需要 E2E_ADMIN_PASS");

test("採信哪些證據：分組且欄位對齊", async ({ page }) => {
  await page.setViewportSize({ width: 1500, height: 1000 });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(process.env.E2E_ADMIN_USER || "admin");
  await page.getByPlaceholder(/密碼|Password/).fill(PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await page.waitForURL((u) => !u.pathname.includes("/login"));

  await page.goto("/system-settings", { waitUntil: "domcontentloaded" });
  // 等元素出現，不要等固定秒數 —— 設定是 onMounted 才去拿的，機器慢一點就會誤判成 0 組
  const rows = page.locator(".ss-src-row");
  await expect(rows.first()).toBeVisible({ timeout: 20_000 });
  expect(await rows.count(), "應該至少有「探測／監控」與「ARP 表」兩組").toBeGreaterThan(1);

  // 每一列第一格的 x 相同 → 各列真的對齊（不是靠看的）
  const firstXs: number[] = [];
  for (let i = 0; i < await rows.count(); i++) {
    const box = await rows.nth(i).locator(".ss-src-item").first().boundingBox();
    firstXs.push(Math.round(box!.x));
  }
  expect(new Set(firstXs).size, `各列起點不一致：${firstXs.join(", ")}`).toBe(1);

  // 內部鍵不可以露出來（使用者回報看到全小寫的 paloalto）
  const text = await page.locator(".ss-src-grid").first().innerText();
  expect(text).not.toMatch(/paloalto|pfsense|fortigate|opnsense/);
});
