import { test, expect, type Page } from "@playwright/test";

/**
 * 系統診斷（管理 → 系統診斷）。
 *
 * 由來（2026-09-05 客戶回報）：儀表板數得出 55 台裝置、裝置清單卻是 Internal Server Error。
 * 診斷過程中請客戶跑指令與貼 SQL 的每一件事，都是我們自己該做的 —— 所以有了這一頁。
 */
const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";

test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS env 才能跑");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

test.describe("系統診斷", () => {
  test("列出檢查項目，且每個非正常項目都講得出怎麼修", async ({ page }) => {
    await login(page);
    await page.goto("/doctor");
    await expect(page.getByText("系統診斷").first()).toBeVisible({ timeout: 10_000 });
    // 資料庫結構是最重要的一項（客戶那次的第一嫌疑），一定要在
    await expect(page.getByText("資料庫結構")).toBeVisible({ timeout: 20_000 });
    // 資料健檢：拿正式讀取 schema 驗每一列，綠燈才代表清單頁畫得出來
    await expect(page.getByText("資料健檢")).toBeVisible();

    // 有問題的項目一定要附「怎麼修」——只說壞了等於沒說
    const problems = page.locator('.doc-row[data-status="bad"], .doc-row[data-status="warn"]');
    for (let i = 0; i < await problems.count(); i++) {
      await expect(problems.nth(i).locator(".doc-fix")).toBeVisible();
    }

    // 後端看不到系統層的東西，頁面要明講，不能讓人以為全綠＝一切正常
    await expect(page.getByText(/jt-ipam\.sh doctor/)).toBeVisible();
  });

  test("資料統計：各類資料的筆數都是數字，並寫明不會傳送出去", async ({ page }) => {
    await login(page);
    await page.goto("/doctor");
    const st = page.getByTestId("doctor-stats");
    await expect(st).toBeVisible({ timeout: 20_000 });
    await expect(st).toContainText("不會傳送到任何地方");
    for (const key of ["subnets", "ipv4", "devices", "audit_logs"]) {
      await expect(st.locator(`.doc-stat[data-key="${key}"] .doc-stat-v`)).toHaveText(/^(約 )?[\d,]+$/);
    }
    // 時間參數換成觀看者的時區（以前直接印 UTC）：背景作業那一列不可以有 T 或 +00:00
    const jobs = page.locator(".doc-row", { hasText: "背景作業" });
    if (await jobs.count()) await expect(jobs.first()).not.toContainText(/\dT\d|\+00:00/);
  });

  test("容量用量：資料庫依功能的分布圖、主機上的目錄、最大的資料表", async ({ page }) => {
    // 使用者 2026-10-14：「各個功能吃掉多少容量、資料庫吃多少」「最好還要多個分布圖，一目了然」
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page);
    await page.goto("/doctor");
    const su = page.getByTestId("doctor-storage");
    await expect(su).toBeVisible({ timeout: 20_000 });
    await expect(su).toContainText("不會傳送到任何地方");
    await expect(su.getByTestId("storage-card-db")).toContainText(/\d+(\.\d+)? (KB|MB|GB)/);
    // 環圈：每個功能一段，圖例同樣的分類、有大小與百分比
    const arcs = su.locator(".su-arc");
    expect(await arcs.count()).toBeGreaterThan(2);
    const legend = su.getByTestId("storage-db-groups").locator(".su-legend li");
    expect(await legend.count()).toBeGreaterThanOrEqual(await arcs.count());
    await expect(legend.filter({ hasText: "IP 位址管理" })).toContainText(/(KB|MB|GB)/);
    await expect(legend.first()).toContainText(/%/);
    // 滑過圖例：中間的字換成那個分類，其他圖例變淡（圓弧的中心點是環圈的空洞，滑鼠要停在弧線上，改用圖例測）
    await legend.first().hover();
    await expect(su.locator(".su-donut-label").first()).not.toHaveText("資料庫");
    await expect(legend.nth(1)).toHaveClass(/dim/);
    // 主機上的目錄：至少有程式與套件（測試環境沒有 /var/backups 也要講出來）
    await expect(su.getByTestId("storage-dirs")).toContainText("程式與套件");
    // 最大的資料表：由大到小、標出分類
    const rows = su.getByTestId("storage-tables").locator(".su-trow");
    expect(await rows.count()).toBeGreaterThan(3);
    await expect(page.locator("body")).not.toContainText(/doctor\.storage_/);
    if (process.env.E2E_SHOTS_DIR) await page.locator(".n-card").first().screenshot({ path: `${process.env.E2E_SHOTS_DIR}/doctor-storage.png` });
  });

  test("資料統計：一組一張卡，0 筆的淡化", async ({ page }) => {
    await login(page);
    await page.goto("/doctor");
    const st = page.getByTestId("doctor-stats");
    await expect(st).toBeVisible({ timeout: 20_000 });
    expect(await st.locator(".ds-card").count()).toBeGreaterThanOrEqual(4);
    const zero = st.locator(".doc-stat.zero").first();
    if (await zero.count()) await expect(zero.locator(".doc-stat-v")).toHaveText("0");
  });

  test("可以下載純文字記錄檔", async ({ page }) => {
    await login(page);
    await page.goto("/doctor");
    await expect(page.getByText("資料庫結構")).toBeVisible({ timeout: 20_000 });
    const dl = page.waitForEvent("download");
    await page.getByRole("button", { name: "下載記錄檔" }).click();
    const file = await dl;
    expect(file.suggestedFilename()).toMatch(/^jt-ipam-doctor-.*\.txt$/);
  });

  test("窄視窗時不會橫向溢出", async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    await login(page);
    await page.goto("/doctor");
    await expect(page.getByText("資料庫結構")).toBeVisible({ timeout: 20_000 });
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, "頁面出現橫向捲動").toBeLessThanOrEqual(1);
  });
});
