/**
 * 子網路指示計（使用者 2026-10-11）：
 *   1. 卡片標題列右邊的拉桿可以縮放格子大小，重新整理後還記得
 *   2. 位址範圍（DHCP 集區）的底線在淺色主題要看得清楚：換成深一點的顏色、上面墊一條白色分隔線
 *   3. 底線畫在背景裡，色塊下緣是平的（2026-10-13）；圖例跟著格子大小放大
 * 用 seed_e2e 的 10.20.0.0/24；測試建一個暫時的 DHCP 範圍，測完刪掉。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const API = process.env.E2E_API_URL || "http://127.0.0.1:8010";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function ok(res: any, what: string) {
  if (!res.ok()) throw new Error(`${what} 失敗 HTTP ${res.status()}：${await res.text()}`);
  return res;
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
}

test("格子大小拉桿；淺色主題的 DHCP 底線看得清楚", async ({ page, request }) => {
  const r = await ok(await request.post(`${API}/api/v1/auth/login`, {
    data: { username: ADMIN_USER, password: ADMIN_PASS } }), "登入");
  const h = { Authorization: `Bearer ${(await r.json()).access_token}` };
  const subs = await (await ok(await request.get(`${API}/api/v1/subnets?page_size=500`, { headers: h }), "列出網段")).json();
  const sub = (subs.items ?? []).find((s: any) => String(s.cidr) === "10.20.0.0/24");
  if (!sub) throw new Error("找不到 seed_e2e 的 10.20.0.0/24（先跑 seed_e2e）");
  const rng = await (await ok(await request.post(`${API}/api/v1/subnets/${sub.id}/ranges`, {
    headers: h, data: { start_ip: "10.20.0.200", end_ip: "10.20.0.210", purpose: "dhcp", name: "e2e-grid-dhcp" } }),
  "建立 DHCP 範圍")).json();
  try {
    await login(page);
    await page.evaluate(() => { try { localStorage.removeItem("jt-ipam:grid_cell_size"); } catch { /* 無 */ } });
    await page.goto(`/subnets/${sub.id}`);
    const grid = page.getByTestId("subnet-grid");
    const cell = grid.locator(".cell").first();
    await expect(cell).toBeVisible({ timeout: 20_000 });
    expect(Math.round((await cell.boundingBox())!.width)).toBe(14);
    // 預設大小時圖例維持原本的 10px 色塊、12px 字
    expect(Math.round((await grid.locator(".legend-item i").first().boundingBox())!.width)).toBe(10);
    expect(await grid.locator(".legend-item").first().evaluate((el) => getComputedStyle(el).fontSize)).toBe("12px");

    // 拉到最大：點拉桿最右端
    const slider = page.getByTestId("grid-cell-size");
    const box = (await slider.boundingBox())!;
    await slider.click({ position: { x: box.width - 1, y: box.height / 2 } });
    await expect.poll(async () => Math.round((await cell.boundingBox())!.width)).toBe(32);
    await page.reload();
    await expect.poll(async () => Math.round((await grid.locator(".cell").first().boundingBox())!.width),
      { timeout: 20_000 }).toBe(32);

    // 淺色主題：DHCP 範圍的格子底線是深藍、上面一條白線（以前是亮青色，壓在綠色格子上看不見）
    await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
    const dhcpCell = grid.locator('.cell[data-range="dhcp"]').first();
    await expect(dhcpCell).toBeVisible();
    // 底線畫在背景裡（硬切的色帶），不是內陰影：內陰影的內緣會跟著格子的圓角彎，
    // 色塊在底線上方多出圓角、看起來像有兩個下緣（使用者 2026-10-13）
    const bg = await dhcpCell.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { image: cs.backgroundImage, shadow: cs.boxShadow };
    });
    expect(bg.shadow).toBe("none");
    expect(bg.image).toContain("rgb(3, 105, 161)");
    expect(bg.image).toContain("rgb(255, 255, 255)");
    // 粗細跟著格子大小（32px → 7px，上面 1px 白線）
    expect(bg.image).toMatch(/7px/);
    expect(bg.image).toMatch(/8px/);

    // 圖例的色塊與文字跟著格子大小放大（14px 時是 10px／12px）
    const sw = grid.locator(".legend-item i").first();
    const swPx = (await sw.boundingBox())!.width;
    const fontPx = parseFloat(await grid.locator(".legend-item").first().evaluate((el) => getComputedStyle(el).fontSize));
    expect(swPx).toBeGreaterThanOrEqual(20);
    expect(fontPx).toBeGreaterThanOrEqual(16);
  } finally {
    await request.delete(`${API}/api/v1/subnets/${sub.id}/ranges/${rng.id}`, { headers: h });
  }
});
