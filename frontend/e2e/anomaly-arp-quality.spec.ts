/**
 * ARP 資料品質（2026-10-09 一次 ARP 異常調查的回饋）。樣本由 tests/seed_e2e.py 建立：
 * - 10.20.0.50：雙網卡主機 arpflux-host-01，三台設備回報真實 MAC、路由器回報兩個拼接出來的假 MAC
 *   → 不是 IP 衝突，列在「同一台主機多張網卡（ARP flux）」，假 MAC 標「疑似讀壞」，附 sysctl 修法
 * - 交換器 core-sw-e2e 的 VLAN 1 學到 10.20.0.0/24 與 203.0.113.0/24 的 MAC → 子網段混在同一個二層
 * - 10.20.0.60：第二台機器只有一個來源回報 → IP 衝突，可信度「低」
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
}

test("ARP flux、子網段混用、疑似讀壞的 MAC 與衝突可信度", async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);
  await page.goto("/anomaly?tab=arp_flux");
  await page.getByRole("button", { name: "執行偵測" }).click();

  const flux = page.locator("tr", { hasText: "10.20.0.50" }).first();
  await expect(flux).toBeVisible({ timeout: 30_000 });
  await expect(flux).toContainText("arpflux-host-01");
  await expect(flux).toContainText("疑似讀壞");
  await expect(flux).toContainText("net.ipv4.conf.all.arp_ignore=1");
  await expect(flux).toContainText("網卡：203.0.113.50");
  // 誰回報的：滑過「N 個來源」看得到是哪台設備
  const rep = flux.getByTestId("anm-mac-reporters").filter({ hasText: "1 個來源" }).first();
  await expect(rep).toHaveAttribute("title", /edge-router-e2e/);

  await page.locator(".n-tabs-tab", { hasText: "子網段混在同一個二層" }).click();
  const bleed = page.locator("tr", { hasText: "10.20.0.0/24" }).filter({ hasText: "203.0.113.0/24" }).first();
  await expect(bleed).toBeVisible();
  await expect(bleed).toContainText("core-sw-e2e");
  await expect(bleed).toContainText("10.20.0.50");

  await page.locator(".n-tabs-tab", { hasText: /^\s*IP 衝突/ }).click();
  const low = page.locator("tr", { hasText: "10.20.0.60" }).first();
  await expect(low).toBeVisible();
  await expect(low).toContainText("低");
  await expect(page.locator("tr", { hasText: "10.20.0.50" })).toHaveCount(0);

  // MAC 歷程：拼接出來的 MAC 只有一個來源看過
  await page.goto("/mac/02:00:5e:40:00:b2");
  await expect(page.getByTestId("mh-arp-reporters").first()).toContainText("1 個來源");
});
