/**
 * 全站搜尋的設定（管理 → 系統設定 → 全站搜尋，使用者 2026-10-13）：
 *   - 隨打即找關掉：打字不會出結果，按 Enter 才搜尋
 *   - 關掉的類型不出現；排列順序照設定（IP 位址移到最前面就排第一組）
 *   - 還原預設後恢復隨打即找、所有類型
 * 會改全站設定 → 登記在 global-state-specs.txt（發版 e2e 依序跑），跑完還原成原本的設定。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function token(request: APIRequestContext): Promise<Record<string, string>> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok(), await r.text()).toBe(true);
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
}

test("全站搜尋設定：按 Enter 才搜尋、關掉的類型不出現、照設定排列；還原預設", async ({ page, request }) => {
  const H = await token(request);
  const tag = `gsx${Date.now().toString(36)}`;
  const original = await (await request.get("/api/v1/system/search-settings", { headers: H })).json();
  const subs = await (await request.get("/api/v1/subnets?page_size=500", { headers: H })).json();
  const sub = subs.items.find((s: any) => s.cidr === "10.20.0.0/24");
  // 這支專用的位址；先解除冷卻（上一輪刪掉時會進冷卻期，不解除就建不起來）
  const ADDR = "10.20.0.233";
  // 冷卻中就解除（200）；沒在冷卻回 404 也可以；其他狀態碼代表呼叫錯了，要講出來
  const cleared = await request.post(`/api/v1/addresses/cooldowns/${sub.id}/${ADDR}/clear`,
    { headers: H, data: { reason: "e2e global search settings" } });
  expect([200, 404], await cleared.text()).toContain(cleared.status());
  const ipRes = await request.post("/api/v1/addresses", { headers: H, data: {
    subnet_id: sub.id, ip: ADDR, hostname: `${tag}-web` } });
  expect(ipRes.ok(), await ipRes.text()).toBe(true);
  const ip = await ipRes.json();
  const dev = await (await request.post("/api/v1/devices", { headers: H, data: { name: `${tag}-switch`, type: "switch" } })).json();
  try {
    await login(page);
    await page.setViewportSize({ width: 1400, height: 1800 });   // 整份類型清單在畫面內，拖拉才量得到
    await page.goto("/system-settings");
    const card = page.getByTestId("ss-global-search");
    await expect(card.getByTestId("search-type-device")).toBeVisible({ timeout: 20_000 });

    // 排序照既有做法（拖拉把手；把手聚焦後也能按上下方向鍵）
    const order = () => card.locator("[data-search-type]").evaluateAll(
      (els) => els.map((e) => (e as HTMLElement).dataset.searchType));
    // 滑鼠拖拉：機櫃拖到最上面
    const rackHandle = card.locator("[data-search-type='rack'] .gss-drag");
    const firstRow = card.locator("[data-search-type]").first();
    await card.locator(".rank-list").scrollIntoViewIfNeeded();
    await rackHandle.hover();
    const hb = (await rackHandle.boundingBox())!;
    const tb = (await firstRow.boundingBox())!;
    await page.mouse.down();
    await page.mouse.move(hb.x + hb.width / 2, tb.y + 2, { steps: 8 });
    await page.mouse.up();
    expect((await order())[0]).toBe("rack");
    // 鍵盤：IP 位址的把手按向上，一路移到最前面
    const ipHandle = card.locator("[data-search-type='ip_address'] .gss-drag");
    await ipHandle.focus();
    for (let i = 0; i < 8; i++) await page.keyboard.press("ArrowUp");
    expect((await order())[0]).toBe("ip_address");
    // 關掉裝置（右側開關）、關掉隨打即找，存檔
    await card.getByTestId("search-type-enabled-device").click();
    await card.getByTestId("search-settings-instant").click();
    // 等存檔的回應本身：畫面上的「已儲存」訊息可能是上一次存檔留下的
    const put1 = page.waitForResponse((r) => r.url().includes("/api/v1/system/search-settings")
      && r.request().method() === "PUT");
    await card.getByTestId("search-settings-save").click();
    expect((await put1).ok()).toBe(true);
    const saved = await (await request.get("/api/v1/system/search-settings", { headers: H })).json();
    expect(saved.instant).toBe(false);
    expect(saved.types[0].type).toBe("ip_address");
    expect(saved.types.find((x: any) => x.type === "device").enabled).toBe(false);

    // 重新載入讓搜尋框拿到新設定；打字不會出結果，按 Enter 才搜尋
    await page.reload();
    const box = page.locator(".global-search__input input");
    await box.fill(tag);
    await page.waitForTimeout(900);
    await expect(page.locator(".n-base-select-option", { hasText: `${tag}-web` })).toHaveCount(0);
    await box.press("Enter");
    await expect(page.locator(".n-base-select-option", { hasText: `${tag}-web` })).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".n-base-select-option", { hasText: `${tag}-switch` })).toHaveCount(0);
    await expect(page.locator(".n-base-select-group-header").first()).toHaveText(/IP 位址|IP addresses/i);

    // 還原預設：隨打即找、裝置又搜得到
    await page.keyboard.press("Escape");
    await page.goto("/system-settings");
    await expect(card.getByTestId("search-settings-reset")).toBeEnabled({ timeout: 20_000 });
    await card.getByTestId("search-settings-reset").click();
    const put2 = page.waitForResponse((r) => r.url().includes("/api/v1/system/search-settings")
      && r.request().method() === "PUT");
    await card.getByTestId("search-settings-save").click();
    expect((await put2).ok()).toBe(true);
    await page.reload();
    await page.locator(".global-search__input input").fill(tag);
    await expect(page.locator(".n-base-select-option", { hasText: `${tag}-switch` })).toBeVisible({ timeout: 10_000 });
  } finally {
    await request.put("/api/v1/system/search-settings", { headers: H, data: { instant: original.instant, types: original.types } });
    if (dev?.id) await request.delete(`/api/v1/devices/${dev.id}`, { headers: H });
    if (ip?.id) await request.delete(`/api/v1/addresses/${ip.id}`, { headers: H });
    await request.post(`/api/v1/addresses/cooldowns/${sub.id}/${ADDR}/clear`,
      { headers: H, data: { reason: "e2e global search settings" } });
  }
});
