/**
 * 使用者 2026-10-12：
 *   1. IP 位址頁的欄位要比照子網路詳細頁的 IP 表（以前缺 DHCP、製造商、裝置、說明、最後出現、失聯）
 *   2. IP 異動記錄頁：IP 可以點進詳細資料、欄位標題可以排序（伺服器端分頁，排序要送到後端）
 * 用 seed_e2e 的樣本資料；欄位偏好在測試後還原預設值。
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

const NEW_COLUMNS = ["DHCP", "製造商", "裝置", "說明", "最後出現", "失聯"];

test("IP 位址頁的欄位跟子網路頁一樣，新欄位可以在伺服器端排序", async ({ page }) => {
  await login(page);
  await page.goto("/addresses");
  await expect(page.locator(".n-data-table-tr").nth(1)).toBeVisible({ timeout: 20_000 });
  await page.getByTitle("欄位").first().click();
  const picker = page.locator(".col-picker");
  await expect(picker).toBeVisible();
  for (const label of NEW_COLUMNS) {
    await expect(picker.locator(".col-picker-row").filter({ hasText: new RegExp(`^\\s*${label}\\s*$`) })).toHaveCount(1);
  }
  try {
    await picker.locator(".col-picker-row").filter({ hasText: /^\s*最後出現\s*$/ }).locator(".n-checkbox").click();
    await page.keyboard.press("Escape");
    const header = page.locator("th").filter({ hasText: "最後出現" });
    await expect(header).toBeVisible();
    const req = page.waitForRequest((r) => r.url().includes("/api/v1/addresses?") && r.url().includes("sort=last_seen"));
    await header.click();
    await req;
  } finally {
    await page.getByTitle("欄位").first().click();
    await page.locator(".col-picker").getByText("還原預設值").click();
  }
});

test("IP 異動記錄：IP 點進詳細資料，欄位標題排序送到後端", async ({ page }) => {
  await login(page);
  await page.goto("/ip-changes");
  const link = page.locator("a.ipchg-ip-link").first();
  await expect(link).toBeVisible({ timeout: 20_000 });

  // 標題排序（naive-ui 第一次點是降冪、再點是升冪）：後端收到 sort=ip，畫面的 IP 照位址大小排
  const ipNums = async () => (await page.locator("td:nth-child(2)").allInnerTexts())
    .map((s) => s.trim()).filter((s) => /^\d+\.\d+\.\d+\.\d+$/.test(s))
    .map((s) => s.split(".").reduce((acc, o) => acc * 256 + Number(o), 0));
  const header = page.locator("th").filter({ hasText: /^IP$/ });
  for (const [order, ok] of [["desc", (a: number, b: number) => a >= b], ["asc", (a: number, b: number) => a <= b]] as const) {
    const req = page.waitForRequest((r) => r.url().includes("/api/v1/ip-changes?") && r.url().includes("sort=ip")
      && r.url().includes(`order=${order}`));
    await header.click();
    await req;
    await expect.poll(async () => {
      const nums = await ipNums();
      return nums.length > 1 && nums.every((n, i) => i === 0 || ok(nums[i - 1], n));
    }).toBe(true);
  }

  // 點 IP → IP 詳細資料
  const ipText = (await page.locator("a.ipchg-ip-link").first().innerText()).trim();
  await page.locator("a.ipchg-ip-link").first().click();
  await expect(page).toHaveURL(/\/addresses\/[0-9a-f-]{36}$/);
  await expect(page.getByText(ipText).first()).toBeVisible();
});
