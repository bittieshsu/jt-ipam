/**
 * Check Point（R81.20，Management API 唯讀，使用者 2026-10-08）。兩種跑法：
 *   - 模擬伺服器：`backend/tests/checkpoint_mock.py` 的 CheckPointMock 起在本機，被測後端要設
 *     OUTBOUND_ALLOW_CIDRS=127.0.0.1/32；E2E_CP_URL=http://127.0.0.1:<port> E2E_CP_KEY=cp-key-0123456789abcdef
 *   - 實機：E2E_CP_URL=https://<管理伺服器> E2E_CP_KEY=<Read Only All 管理員的 API key>（自簽憑證設 E2E_CP_INSECURE=1）
 * 沒給就跳過（其餘行為由後端測試涵蓋）。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const URL = process.env.E2E_CP_URL || "";
const KEY = process.env.E2E_CP_KEY || "";
const INSECURE = process.env.E2E_CP_INSECURE === "1";
test.skip(!ADMIN_PASS || !URL || !KEY, "需要 E2E_ADMIN_PASS、E2E_CP_URL、E2E_CP_KEY");

// 閘道的 Gaia API（第二階段）：唯讀角色的帳號。沒給就跳過那一個測試
const GAIA_URL = process.env.E2E_CPG_URL || "";
const GAIA_USER = process.env.E2E_CPG_USER || "";
const GAIA_PASS = process.env.E2E_CPG_PASS || "";

const TAG = `cp-e2e-${Date.now().toString(36)}`;

async function token(request: APIRequestContext): Promise<string> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).access_token;
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

test("Check Point：新增、測試連線講出 API 版本、立即同步、規則／物件／閘道頁籤有資料、搜尋走後端", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await login(page);
  await page.goto("/checkpoint");
  await page.getByTestId("cp-create").click();
  await page.getByTestId("cp-name").locator("input").fill(TAG);
  await page.getByTestId("cp-url").locator("input").fill(URL);
  // 沒填 API key 存不了
  await page.getByTestId("cp-save").click();
  await expect(page.locator(".n-message")).toContainText(/API key/);
  await page.getByTestId("cp-secret").locator("input").fill(KEY);
  if (INSECURE) await page.locator(".n-modal .n-form-item", { hasText: /驗證 TLS/ }).locator(".n-switch").click();
  await page.getByTestId("cp-save").click();
  const row = page.getByTestId("cp-list").locator("tr", { hasText: TAG });
  await expect(row).toBeVisible();

  await row.getByTestId("cp-test").click();
  await expect(page.locator(".n-message").filter({ hasText: /API \d/ })).toBeVisible({ timeout: 60_000 });

  await row.getByTestId("cp-sync").click();
  await expect.poll(async () => {
    await page.getByRole("button", { name: /重新整理/ }).first().click();
    return row.innerText();
  }, { timeout: 120_000 }).toMatch(/規則 \d+/);

  const pick = async () => {
    const sel = page.locator(".n-tabs-pane-wrapper .n-select").first();
    if (await sel.count() && await sel.isVisible()) {
      await sel.click();
      await page.locator(".n-base-select-option", { hasText: TAG }).click();
    }
  };
  await page.locator(".n-tabs-tab").filter({ hasText: "存取規則" }).click();
  await pick();
  const rules = page.getByTestId("cp-rules");
  await expect(rules.locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });
  const total = await rules.locator("tbody tr").count();
  // 搜尋一個一定搜不到的字：清單要變空（證明是後端搜尋，不是只篩目前這一頁）
  await page.getByTestId("cp-search-rules").locator("input").fill("zz-no-such-rule-zz");
  await expect(rules.locator("tbody tr.n-data-table-tr")).toHaveCount(0, { timeout: 10_000 });
  await page.getByTestId("cp-search-rules").locator("input").fill("");
  await expect.poll(() => rules.locator("tbody tr.n-data-table-tr").count(), { timeout: 10_000 }).toBe(total);

  await page.locator(".n-tabs-tab").filter({ hasText: "網路物件" }).click();
  await expect(page.getByTestId("cp-objects").locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });
  await page.locator(".n-tabs-tab").filter({ hasText: "閘道" }).click();
  await expect(page.getByTestId("cp-gateways").locator("tbody tr").first()).toBeVisible({ timeout: 15_000 });

  // 收尾：刪掉（也收回它寫進共用 NAT 表的資料）
  const list = await (await request.get("/api/v1/checkpoint/servers", { headers: auth })).json();
  const mine = list.items.find((x: { name: string }) => x.name === TAG);
  expect((await request.delete(`/api/v1/checkpoint/servers/${mine.id}`, { headers: auth })).status()).toBe(204);
});

test("Check Point Gaia：預設就讀 ARP 與租約；唯讀帳號照讀 DHCP 設定，ARP 與租約標成略過、不算同步失敗", async ({ page, request }) => {
  // 使用者 2026-10-08：「抓 dhcp 跟 arp 直接實作，抓不到沒關係，給客戶測」
  test.skip(!GAIA_URL || !GAIA_USER || !GAIA_PASS, "需要 E2E_CPG_URL、E2E_CPG_USER（Gaia 唯讀角色）、E2E_CPG_PASS");
  test.setTimeout(300_000);       // 實機：管理伺服器與閘道各同步一次
  const auth = { Authorization: `Bearer ${await token(request)}` };
  const name = `${TAG}-gaia`;
  const cr = await request.post("/api/v1/checkpoint/servers", { headers: auth,
    data: { name, api_url: URL, verify_tls: !INSECURE, secret: KEY } });
  expect(cr.status()).toBe(201);
  const srv = await cr.json();
  try {
    await request.post(`/api/v1/checkpoint/servers/${srv.id}/sync`, { headers: auth });
    await expect.poll(async () => {
      const l = await (await request.get("/api/v1/checkpoint/servers", { headers: auth })).json();
      return l.items.find((x: { id: string }) => x.id === srv.id)?.last_sync_at ?? null;
    }, { timeout: 120_000 }).not.toBeNull();
    const gws = await (await request.get(`/api/v1/checkpoint/servers/${srv.id}/gateways`, { headers: auth })).json();
    const gw = gws.find((g: { type: string }) => g.type === "simple-gateway") ?? gws[0];
    // 沒帶 allow_scripts：預設就要讀 ARP 與租約
    const tr = await request.post(`/api/v1/checkpoint/servers/${srv.id}/gaia-targets`, { headers: auth,
      data: { name: gw.name, gaia_url: GAIA_URL, username: GAIA_USER, secret: GAIA_PASS, gateway_uid: gw.uid,
              verify_tls: !INSECURE } });
    expect(tr.status()).toBe(201);
    const tgt = await tr.json();
    expect(tgt.allow_scripts).toBe(true);
    await request.post(`/api/v1/checkpoint/gaia-targets/${tgt.id}/sync`, { headers: auth });
    await expect.poll(async () => {
      const l = await (await request.get(`/api/v1/checkpoint/servers/${srv.id}/gaia-targets`, { headers: auth })).json();
      return l[0]?.last_sync_at ?? null;
    }, { timeout: 120_000 }).not.toBeNull();
    const after = (await (await request.get(`/api/v1/checkpoint/servers/${srv.id}/gaia-targets`, { headers: auth })).json())[0];
    expect(after.last_error).toBeNull();
    expect(after.last_summary.skipped.arp).toBe("no_permission");
    // 閘道的 DHCP 伺服器沒開時，租約不用讀就知道沒有（dhcp_off）；有開才會碰到權限
    expect(["no_permission", "dhcp_off"]).toContain(after.last_summary.skipped.leases);

    await login(page);
    await page.goto(`/checkpoint?tab=gateways&fw=${srv.id}`);
    const row = page.getByTestId("cp-gateways").locator("tr", { hasText: gw.name });
    await expect(row).toContainText(/DHCP 子網路 \d+/, { timeout: 15_000 });
    await expect(row).toContainText(/ARP 表(、DHCP 租約)?略過：帳號沒有執行指令的權限/);
    await expect(row).toContainText("唯讀");          // 標籤照實際能做到的，不是照勾選
    // 摘要欄在窄螢幕要橫向捲動；略過的原因在標籤旁邊的圖示就看得到
    await row.getByTestId("cpg-skipped").hover();
    await expect(page.locator(".n-tooltip")).toContainText("帳號沒有執行指令的權限");
    // 設定視窗：讀取 ARP 與租約預設是勾著的
    await row.getByTestId("cpg-edit").click();
    await expect(page.getByTestId("cpg-allow-scripts")).toHaveClass(/n-checkbox--checked/);
  } finally {
    expect((await request.delete(`/api/v1/checkpoint/servers/${srv.id}`, { headers: auth })).status()).toBe(204);
  }
});
