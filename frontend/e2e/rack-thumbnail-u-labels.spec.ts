/**
 * 裝置頁的機櫃縮圖：U 編號每一個都只佔一行（客戶 2026-10-12）。
 *
 * 縮圖會把整張圖等比縮小（42U 大約 0.38 倍），字級再用 --rd-fit 放大補回來；編號欄的寬度原本是固定的
 * 26px，沒有跟著補，兩位數擠不下就折成兩行，十位數疊到下一格，10 到 42 全部糊成一團。
 * 型別與單元測試看不出來，只有量畫面上的編號才抓得到。
 *
 * 自己建一座 42U 機櫃、在 U40 放一台 2U 裝置，跑完刪掉。
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function token(request: APIRequestContext): Promise<Record<string, string>> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok(), await r.text()).toBe(true);
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

test("42U 縮圖的 U 編號不折行、不疊到隔壁", async ({ page, request }) => {
  const H = await token(request);
  const tag = Date.now().toString(36);
  const loc = await (await request.post("/api/v1/locations", { headers: H, data: { name: `e2e-ulabel-${tag}` } })).json();
  const rack = await (await request.post("/api/v1/racks", {
    headers: H, data: { name: `e2e-ulabel-${tag}`, location_id: loc.id, u_height: 42 } })).json();
  const dev = await (await request.post("/api/v1/devices", {
    headers: H, data: { name: `e2e-ulabel-${tag}`, type: "server", location_id: loc.id, rack_id: rack.id,
                        u_position: 40, u_size: 2, rack_face: "front" } })).json();
  try {
    await page.goto("/login");
    await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
    await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
    await page.getByRole("button", { name: /登入/ }).click();
    await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
    await page.setViewportSize({ width: 1700, height: 1100 });
    await page.goto(`/devices/${dev.id}`);
    const labels = page.locator(".dev-head-rack .u-num-out");
    await expect(labels.first()).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(300);   // 等 ResizeObserver 量完、縮放套上去

    const cells = await labels.evaluateAll((els: Element[]) => els.map((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      return { text: (el.textContent || "").trim(), lines: range.getClientRects().length,
               textH: range.getBoundingClientRect().height, cellH: el.getBoundingClientRect().height };
    }));
    expect(cells.length).toBe(42);
    const wrapped = cells.filter((c) => c.lines > 1 || c.textH > c.cellH * 1.6);
    expect(wrapped.map((c) => c.text), "這些編號折成了兩行").toEqual([]);
    expect(cells.some((c) => c.text === "42")).toBe(true);
  } finally {
    await request.delete(`/api/v1/devices/${dev.id}`, { headers: H });
    await request.post("/api/v1/racks/bulk-delete", { headers: H, data: { ids: [rack.id] } });
    await request.post("/api/v1/locations/bulk-delete", { headers: H, data: { ids: [loc.id] } });
  }
});
