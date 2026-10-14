/**
 * ISOinsight 整合頁。
 *
 * - 新增來源（時區要勾確認才能存）→ 列表不顯示密碼、操作欄固定在右邊看得到
 * - 測試連線：分階段結果、標示「尚未套用」；連不上時講出是哪一個階段、什麼原因
 * - 有 E2E_ISOINSIGHT_URL（一台會回租約 JSON 的測試用 ISOinsight）時，再走完預覽 → 立即同步 → 租約與同步記錄頁籤
 *
 * 只用文件用位址；真機的登入回應、方法與時區仍待驗證（TEST_CHECKLIST 7b5）。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const ISO_URL = process.env.E2E_ISOINSIGHT_URL || "";
const SHOTS = process.env.E2E_SHOTS_DIR || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

const SUFFIX = Date.now().toString(36);

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

async function noRawKeys(page: Page) {
  // 動態組出來的 i18n 鍵少翻譯時會原樣顯示（isoinsight.xxx）
  await expect(page.locator("body")).not.toContainText(/isoinsight\.[a-z_]+/);
  await expect(page.locator("body")).not.toContainText(/errors\.isoinsight_/);
}

test("ISOinsight：新增、測試連線、（有測試設備時）預覽與同步、刪除", async ({ page }) => {
  test.setTimeout(180_000);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  await page.goto("/isoinsight");
  await expect(page.getByText("ISOinsight 整合").first()).toBeVisible();
  await expect(page.getByText("待真機驗證").first()).toBeVisible();

  // ── 新增 ──
  await page.getByTestId("iso-create").click();
  const name = `iso-e2e-${SUFFIX}`;
  await page.getByTestId("iso-name").locator("input").fill(name);
  await page.getByTestId("iso-url").locator("input").fill(ISO_URL || "http://192.0.2.199");
  await page.getByTestId("iso-user").locator("input").fill("ipam-ro");
  await page.getByTestId("iso-pw").locator("input").fill("e2e pass&=word");
  await page.getByTestId("iso-subnets").click();
  const opts = page.locator(".n-base-select-option");
  await expect(opts.first()).toBeVisible();
  const hq = opts.filter({ hasText: "192.0.2.0/24" });
  await ((await hq.count()) ? hq.first() : opts.first()).click();
  await page.keyboard.press("Escape");
  // 時區沒勾確認就存不了
  await expect(page.getByTestId("iso-save")).toBeDisabled();
  await page.getByTestId("iso-tz-ok").click();
  await shot(page, "01-form");
  await page.getByTestId("iso-save").click();
  const row = page.locator("tr").filter({ hasText: name });
  await expect(row).toBeVisible();
  await expect(page.locator(".n-modal")).toHaveCount(0);
  await expect(row).not.toContainText("e2e pass");
  // 排程預設開啟（使用者 2026-10-08）；還沒成功預覽前清單標「待重新預覽」，排程輪到也不會同步
  await expect(row).toContainText("每 5 分鐘");
  await expect(row).toContainText("待重新預覽");

  // 操作欄固定在右邊：按鈕在視窗內看得到
  const box = await row.getByTestId("iso-sync").boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x + box!.width).toBeLessThanOrEqual(1440);
  await shot(page, "02-sources");

  // ── 測試連線 ──
  await row.getByTestId("iso-test").click();
  await expect(page.getByTestId("iso-not-applied")).toBeVisible();
  await page.getByTestId("iso-probe-run").click();
  const result = page.getByTestId("iso-probe-result");
  await expect(result).toBeVisible({ timeout: 60_000 });
  if (ISO_URL) {
    await expect(result).toContainText("連線與讀取成功");
    await expect(page.locator(".n-modal")).toContainText("收到 Cookie");
  } else {
    // 連不上：講出階段與原因（不是一句「失敗」）
    await expect(result).toContainText("登入");
  }
  await noRawKeys(page);
  await shot(page, "03-test");
  await page.keyboard.press("Escape");
  await expect(page.locator(".n-modal")).toHaveCount(0);

  if (ISO_URL) {
    // ── 預覽：尚未套用 ──
    await row.getByTestId("iso-preview").click();
    await page.getByTestId("iso-probe-run").click();
    await expect(page.getByTestId("iso-preview-rows")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("iso-not-applied")).toContainText("尚未套用");
    await expect(page.locator(".n-modal")).toContainText("會新增 IP");
    await expect(page.locator(".n-modal")).toContainText("未配對");
    await noRawKeys(page);
    await shot(page, "04-preview");
    await page.keyboard.press("Escape");
    await expect(page.locator(".n-modal")).toHaveCount(0);

    // ── 預覽成功後排程就會同步（開關本來就開著）；編輯時密碼留空＝保留（要勾「更換密碼」才能改）──
    await expect(row).not.toContainText("待重新預覽");
    await row.getByTestId("iso-edit").click();
    await expect(page.getByTestId("iso-pw").locator("input")).toBeDisabled();
    await expect(page.getByTestId("iso-change-pw")).toBeVisible();
    const sched = page.getByTestId("iso-schedule");
    await expect(sched).toHaveClass(/n-switch--active/);
    await expect(sched).not.toHaveClass(/n-switch--disabled/);
    await shot(page, "04b-edit");
    await page.getByTestId("iso-save").click();
    await expect(page.locator(".n-modal")).toHaveCount(0);
    await expect(row).toContainText("每 5 分鐘");

    // ── 立即同步 ──
    await row.getByTestId("iso-sync").click();
    await expect(page.locator(".n-message").filter({ hasText: /ISOinsight/ })).toBeVisible();
    await expect.poll(async () => {
      await page.getByRole("button", { name: /重新整理/ }).first().click();
      return await row.innerText();
    }, { timeout: 60_000 }).toMatch(/部分成功|成功/);
    await shot(page, "05-after-sync");

    // ── 租約頁籤 ──
    await page.locator(".n-tabs-tab").filter({ hasText: "租約" }).click();
    const leases = page.getByTestId("iso-leases");
    await expect(leases).toContainText("192.0.2.20");
    await expect(leases).toContainText("租約期間內");
    await expect(leases).toContainText("MAC 衝突");
    await noRawKeys(page);
    await shot(page, "06-leases");

    // ── 同步記錄 ──
    await page.locator(".n-tabs-tab").filter({ hasText: "同步記錄" }).click();
    await expect(page.getByTestId("iso-runs")).toContainText(/同步/);
    await noRawKeys(page);
    await shot(page, "07-runs");

    // ── 作業頁：結果類型要講出來（部分成功不可以看起來跟成功一樣）──
    await page.goto("/tasks");
    await page.locator(".n-tabs-tab").filter({ hasText: "歷史" }).click();
    await expect(page.locator("body")).toContainText(/isoinsight\.sync/);
    await expect(page.getByTestId("task-summary-text").filter({ hasText: /取得 \d+/ }).first()).toBeVisible();
    await shot(page, "08-tasks");
    await page.goto("/isoinsight");
  }

  // ── 刪除 ──
  await row.getByRole("button", { name: "刪除" }).click();
  await page.mouse.move(5, 5);
  await page.locator(".n-popconfirm").getByRole("button", { name: /確定|確認|OK/ }).click();
  await expect(page.locator("tr").filter({ hasText: name })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("ISOinsight：缺欄位講清楚缺什麼；後端欄位檢查失敗翻成「欄位：原因」", async ({ page }) => {
  // 2026-10-12 客戶沒選允許同步的子網路，只看到「Invalid request」
  test.setTimeout(90_000);
  await login(page);
  await page.goto("/isoinsight");
  await page.getByTestId("iso-create").click();
  await page.getByTestId("iso-name").locator("input").fill(`iso-e2e-422-${SUFFIX}`);
  await page.getByTestId("iso-url").locator("input").fill("http://192.0.2.199");
  await page.getByTestId("iso-tz-ok").click();
  const toast = page.locator(".n-message").last();

  await page.getByTestId("iso-save").click();
  await expect(toast).toContainText("請填寫：帳號；密碼");

  await page.getByTestId("iso-user").locator("input").fill("ipam-ro");
  await page.getByTestId("iso-pw").locator("input").fill("e2e-pass");
  await page.getByTestId("iso-save").click();
  await expect(page.locator(".n-message").last()).toContainText("請至少選一個「允許同步的子網路」");

  // 前端檢查都過了、後端仍判定欄位不合（例如 API 規則比畫面嚴）：顯示欄位與原因，不再是「Invalid request」
  await page.getByTestId("iso-subnets").click();
  await page.locator(".n-base-select-option").first().click();
  await page.keyboard.press("Escape");
  let posted = 0;
  await page.route("**/api/v1/isoinsight/sources", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posted++;
    await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({
      detail: "Invalid request",
      errors: [
        { type: "too_short", loc: ["body", "scope_subnet_ids"], msg: "List should have at least 1 item", ctx: { min_length: 1 } },
        { type: "value_error", loc: ["body", "base_url"], msg: "Value error, base_url must start with http:// or https://" },
      ] }) });
  });
  await page.getByTestId("iso-save").click();
  const last = page.locator(".n-message").last();
  await expect(last).toContainText("允許同步的子網路：至少要選 1 項");
  await expect(last).toContainText("Base URL：base_url must start with http:// or https://");
  await expect(last).not.toContainText("Invalid request");
  expect(posted).toBe(1);
  await expect(page.locator(".n-modal")).toHaveCount(1);   // 表單留著讓使用者改
});

test("ISOinsight：新增時預設用原廠說明的登入方式；一鍵套用", async ({ page }) => {
  // 原廠回信（2026-10-12）：POST JSON 到 /logon 取得 SessionID，以網址參數 sessionid 讀租約
  await login(page);
  await page.goto("/isoinsight");
  await page.getByTestId("iso-create").click();
  await page.getByText("進階設定").click();
  await expect(page.getByTestId("iso-token-param").locator("input")).toHaveValue("sessionid");
  await page.getByTestId("iso-token-in").getByText("標頭").click();
  await expect(page.getByTestId("iso-token-param")).toHaveCount(0);
  await expect(page.getByText("Token Header")).toBeVisible();
  await page.getByTestId("iso-vendor-login").click();
  await expect(page.getByTestId("iso-token-param").locator("input")).toHaveValue("sessionid");
  await expect(page.locator(".n-modal").getByText("/logon", { exact: false }).first()).toBeVisible();
  await noRawKeys(page);
});

test("ISOinsight：設備說帳密錯（code 仍是 200）→ 講出設備原文，不叫人改 Token 欄位", async ({ page }) => {
  // 客戶實機 2026-10-14：POST /logon 回 HTTP 200 {msg: "Invalid User/Password", code: "200"}
  await login(page);
  const token = await page.evaluate(() => sessionStorage.getItem("access_token") || "");
  const headers = { Authorization: `Bearer ${token}` };
  const subs = await (await page.request.get("/api/v1/subnets?page_size=50", { headers })).json();
  const created = await page.request.post("/api/v1/isoinsight/sources", { headers, data: {
    name: `iso-refused-${SUFFIX}`, base_url: "https://192.0.2.199", login_path: "/logon", post_format: "json",
    username: "ipam-ro", password: "wrong", auth_mode: "token", token_path: "SessionID", token_in: "query",
    timezone_confirmed: true, scope_subnet_ids: [subs.items[0].id], schedule_enabled: false } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const src = await created.json();
  try {
    await page.route(`**/api/v1/isoinsight/sources/${src.id}/test`, (route) => route.fulfill({ json: {
      ok: false, applied: false, run_id: "00000000-0000-4000-8000-000000000001", error_code: "AUTH_FAILED",
      error: 'login refused by the device: "Invalid User/Password"', stage: "login", http_status: 200,
      stages: [{ stage: "login", method: "POST", post_format: "json", path: "/logon", status: 200, elapsed_ms: 22,
                 login_method: "POST", token_found: false, response_shape: '{msg: "Invalid User/Password", code: "200"}',
                 login_message: "Invalid User/Password" }] } }));
    await page.goto("/isoinsight");
    const row = page.locator("tr", { hasText: `iso-refused-${SUFFIX}` }).first();
    await expect(row).toBeVisible({ timeout: 15_000 });
    await row.getByTestId("iso-test").click();
    await page.getByTestId("iso-probe-run").click();
    const refused = page.getByTestId("iso-login-refused");
    await expect(refused).toContainText("設備拒絕登入：「Invalid User/Password」");
    await expect(refused).toContainText("帳號或密碼錯誤");
    await expect(page.getByTestId("iso-response-shape")).toHaveCount(0);
    await expect(page.locator(".n-modal")).not.toContainText("Token 欄位路徑」，例如");
    // 登入失敗時「用 curl 自己測」自動展開：同樣的登入（原廠方式、關閉 TLS 驗證就加 -k），密碼是佔位字
    const loginCmd = page.getByTestId("iso-curl-login");
    await expect(loginCmd).toBeVisible();
    await expect(loginCmd).toContainText("curl -sS -X POST 'https://192.0.2.199/logon'");
    await expect(loginCmd).toContainText(`-d '{"username":"ipam-ro","password":"<密碼>"}'`);
    await expect(loginCmd).not.toContainText("wrong");
    await expect(page.getByTestId("iso-curl-fetch")).toContainText("--data-urlencode 'sessionid=<SessionID>'");
    if (SHOTS) await page.locator(".n-modal").first().screenshot({ path: `${SHOTS}/iso-curl.png` });
    await noRawKeys(page);
  } finally {
    await page.request.delete(`/api/v1/isoinsight/sources/${src.id}`, { headers });
  }
});
