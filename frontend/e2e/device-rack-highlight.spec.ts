import { test, expect } from "@playwright/test";

/**
 * 裝置詳細頁的機櫃小圖（10-23 客戶）：以前標示本機的方式是把其他裝置疊一層白色淡化，
 * 整櫃變成深淺不一的灰、白字看不到。現在其他裝置維持跟機櫃頁一樣的顏色，本機加粗琥珀框。
 */
const PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!PASS, "需要 E2E_ADMIN_PASS");

test("裝置頁機櫃小圖：本機加框、其他裝置不淡化", async ({ page, request }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(process.env.E2E_ADMIN_USER || "admin");
  await page.getByPlaceholder(/密碼|Password/).fill(PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await page.waitForURL((u) => !u.pathname.includes("/login"));
  const login = await request.post("/api/v1/auth/login",
    { data: { username: process.env.E2E_ADMIN_USER || "admin", password: PASS, realm: "local" } });
  const H = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const devs = (await (await request.get("/api/v1/devices?page_size=500", { headers: H })).json()).items as
    { id: string; rack_id: string | null; u_position: number | null }[];
  const inRack = devs.filter((d) => d.rack_id && d.u_position);
  const byRack = new Map<string, typeof inRack>();
  for (const d of inRack) (byRack.get(d.rack_id!) ?? byRack.set(d.rack_id!, []).get(d.rack_id!)!).push(d);
  const pick = [...byRack.values()].find((l) => l.length >= 2)?.[0];
  test.skip(!pick, "測試資料要有一座放了兩台以上裝置的機櫃");

  await page.goto(`/devices/${pick!.id}`);
  const self = page.locator(".u-part.u-self");
  await expect(self.first()).toBeVisible({ timeout: 20_000 });
  expect(await self.first().evaluate((e) => getComputedStyle(e).boxShadow)).toContain("rgb(245, 158, 11)");
  // 其他裝置沒有疊上淡化的那一層
  const washed = await page.locator(".u-part.u-occupied:not(.u-self)").evaluateAll((els) =>
    els.filter((e) => {
      const a = getComputedStyle(e, "::after");
      return a.content !== "none" && a.backgroundColor !== "rgba(0, 0, 0, 0)";
    }).length);
  expect(washed).toBe(0);
});
