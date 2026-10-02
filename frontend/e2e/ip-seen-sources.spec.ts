/**
 * IP 詳細資料的「各來源最後出現」：獨立一區、三欄對齊（來源／時間／距今），最新的那一列標出來；
 * LibreNMS／Wazuh 的時間可點 → 帶到裝置頁並捲到那張卡片。虛實要分得出 PVE 的 KVM 與 LXC。
 *
 * 種子資料（tests/seed_e2e.py）：10.20.0.40 掛在 seen-host-01，掃描代理 5 分鐘前、LibreNMS 3 小時前、
 * Wazuh 2 天前；PVE 的 LXC 容器 ct-log-01 的網卡是這個位址。
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

async function openIp(page: Page) {
  await page.goto("/addresses?q=10.20.0.40");
  await page.locator("tr", { hasText: "10.20.0.40" }).first().getByText("10.20.0.40").first().click();
  const sec = page.getByTestId("ip-seen-section");
  await expect(sec).toBeVisible({ timeout: 15_000 });
  return sec;
}

test("各來源最後出現獨立一區：固定順序、附距今、最新的那一列標出來；虛實標出 LXC", async ({ page }) => {
  await login(page);
  const sec = await openIp(page);
  await expect(sec.locator("thead")).toContainText("來源");
  await expect(sec.locator("thead")).toContainText("距今");
  const rows = sec.locator("tbody tr");
  await expect(rows.nth(0)).toContainText("掃描代理");
  await expect(rows.nth(1)).toContainText("LibreNMS");
  await expect(rows.nth(1)).toContainText("3 小時前");
  await expect(sec.locator("tr.seen-latest")).toContainText("掃描代理");
  await expect(sec.locator("tr.seen-latest")).toContainText("最新");
  await expect(sec.locator("tr", { hasText: "Wazuh 代理" })).toContainText(/前天|2 天前/);
  // 這些欄位不再散在上面的基本資料裡
  await expect(page.locator(".n-descriptions").first()).not.toContainText("最後出現");
  await expect(page.getByText("容器 · LXC")).toBeVisible();
});

for (const [card, label] of [["librenms", "LibreNMS"], ["wazuh", "Wazuh"]] as const) {
  test(`最後出現（${label}）的時間點下去 → 裝置頁並捲到 ${label} 卡片`, async ({ page }) => {
    await login(page);
    await openIp(page);
    await page.getByTestId(`seen-jump-${card}`).click();
    await expect(page).toHaveURL(new RegExp(`/devices/[0-9a-f-]+\\?card=${card}$`));
    const el = page.locator(`#card-${card}`);
    await expect(el).toHaveClass(/card-focus/, { timeout: 15_000 });
    await expect(el).toBeInViewport();
  });
}
