/**
 * issue #51：OCS 整合的時區。LASTDATE 是 OCS 主機的本地時間、沒有時區，要選 OCS 主機的時區換算才正確。
 * 新增時不選＝跟 jt-ipam 主機相同；選了就照選的存。測試建一個暫時的整合，測完刪掉。
 */
import { test, expect } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const API = process.env.E2E_API_URL || "http://127.0.0.1:8010";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

test("新增 OCS 整合時可以選時區，存進去的就是選的", async ({ page, request }) => {
  const r = await request.post(`${API}/api/v1/auth/login`, { data: { username: ADMIN_USER, password: ADMIN_PASS } });
  const h = { Authorization: `Bearer ${(await r.json()).access_token}` };
  const name = `e2e-ocs-tz-${Date.now().toString(36)}`;
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/ocs");
  await page.getByRole("button", { name: "新增 OCS" }).click();
  const dlg = page.locator(".n-modal");
  await dlg.getByTestId("ocs-name").locator("input").fill(name);
  await dlg.getByTestId("ocs-base-url").locator("input").fill("https://192.0.2.31");
  const tz = dlg.getByTestId("ocs-timezone");
  await tz.scrollIntoViewIfNeeded();
  await expect(tz).toContainText("跟 jt-ipam 主機相同");
  await expect(dlg).toContainText("OCS 主機的本地時間");
  await tz.click();
  await page.keyboard.type("Asia/Tokyo");
  await page.locator(".n-base-select-option", { hasText: /^Asia\/Tokyo$/ }).click();
  await dlg.getByRole("button", { name: /儲存/ }).click();
  await expect(page.locator(".n-data-table-tr", { hasText: name })).toBeVisible({ timeout: 10_000 });
  const items = (await (await request.get(`${API}/api/v1/ocs`, { headers: h })).json()).items;
  const row = items.find((s: any) => s.name === name);
  try {
    expect(row.timezone).toBe("Asia/Tokyo");
  } finally {
    if (row) await request.delete(`${API}/api/v1/ocs/${row.id}`, { headers: h });
  }
});
