/**
 * IP 編輯視窗的「建議建立裝置」。
 *
 * 情境（客戶實際遇到）：一台 DHCP 筆電散在十幾個 IP 上，主機名稱都一樣。
 * 每一筆都手動建裝置、手動關聯是純粹的苦工 —— 但**要不要建立仍然是人決定**，
 * 所以這裡要驗的是：建議會出現、按了才動手、按下去之後同名的 IP 一起接上。
 */
import { test, expect } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const IP_ID = process.env.E2E_DHCP_IP_ID || "";
test.skip(!ADMIN_PASS || !IP_ID, "需要 E2E_ADMIN_PASS 與 E2E_DHCP_IP_ID");
test.describe.configure({ timeout: 120_000 });

async function login(page: any) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
}

test("沒有裝置時給建議，按下去才建立，並一併接上同名的 IP", async ({ page }) => {
  page.on("console", (m) => console.log("[console]", m.type(), m.text().slice(0, 200)));
  page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 300)));
  page.on("response", (r) => {
    if (r.url().includes("device-suggestion")) console.log("[api]", r.status(), r.url());
  });
  await login(page);
  await page.goto(`/addresses/${IP_ID}`);
  // 建議在**編輯模式**的裝置欄下方（使用者要求的位置就是那裡）
  await page.getByRole("button", { name: /^編輯$/ }).click();

  // 1) 建議要出現，而且講得出要建哪一台
  const createBtn = page.getByRole("button", { name: /建立裝置/ });
  await expect(createBtn).toBeVisible({ timeout: 20_000 });
  // 建議要講得出「要建哪一台」，不能只寫「建立裝置」
  await expect(createBtn).toContainText("laptop-07");

  // 2) 同名的其他 IP 有幾筆要講清楚（種了 5 筆，另外 4 筆）
  await expect(page.getByText(/同時關聯另外 4 筆同名 IP/)).toBeVisible();

  // 3) 還沒按之前，不可以已經有裝置 —— 建議不能有副作用
  // ⚠️ page.request 不會帶我們的授權標頭（token 存在 localStorage、不是 cookie），
  //    直接打會拿到 401 的內容，斷言就變成在檢查錯誤訊息（這條踩過）。
  const auth = async () => ({
    Authorization: `Bearer ${await page.evaluate(
      () => localStorage.getItem("access_token") ?? "")}`,
  });
  const before = await page.request.get(
    `/api/v1/addresses/${IP_ID}/device-suggestion`, { headers: await auth() });
  expect(before.status(), "建議端點要能讀到").toBe(200);
  expect((await before.json()).existing_device_id, "看一下建議就把裝置建出來了").toBeNull();

  // 4) 按下去才動手
  await createBtn.click();
  await expect(createBtn).toBeHidden({ timeout: 20_000 });
  await expect(page.getByText("laptop-07").first()).toBeVisible();

  // 5) 同名的 IP 也接上了 —— 再問一次建議，已經沒有未關聯的同名 IP
  const after = await page.request.get(
    `/api/v1/addresses/${IP_ID}/device-suggestion`, { headers: await auth() });
  const body = await after.json();
  expect(body.sibling_unlinked, "同名的 IP 沒有一起接上").toBe(0);
});
