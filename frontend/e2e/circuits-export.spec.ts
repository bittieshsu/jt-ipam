/**
 * issue #50：電路匯出時，供應商與類型輸出成 UUID（例如 Hinet → adbedc6c-…），狀態是 active、頻寬是原始 kbps。
 * 匯出要跟畫面上看到的一樣：名稱、翻好的狀態、格式化的頻寬。
 */
import fs from "node:fs";
import { test, expect, type APIRequestContext } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function token(request: APIRequestContext): Promise<string> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).access_token;
}

test("電路匯出 CSV：供應商、類型是名稱而不是 UUID，狀態與頻寬跟畫面一樣", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  const tag = Date.now().toString(36);
  const provider = await (await request.post("/api/v1/providers", { headers: auth, data: { name: `Hinet-${tag}` } })).json();
  const ctype = await (await request.post("/api/v1/circuit-types", { headers: auth, data: { name: `DSL / VDSL ${tag}` } })).json();
  const cr = await request.post("/api/v1/circuits", { headers: auth, data: {
    cid: `CID-${tag}`, provider_id: provider.id, type_id: ctype.id, status: "active", down_kbps: 100000, up_kbps: 40000 } });
  expect(cr.ok(), await cr.text()).toBeTruthy();
  const circuit = await cr.json();
  try {
    await page.goto("/login");
    await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
    await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
    await page.getByRole("button", { name: "登入", exact: true }).click();
    await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
    await page.goto("/advanced/circuits");
    // 內層的「電路」頁籤（旁邊還有電路供應商、電路類型；外層也有一個同名的）
    await page.locator('.n-tab-pane .n-tabs-tab[data-name="circuits"]').click();
    const pane = page.locator(".n-tab-pane:visible").last();
    await pane.getByPlaceholder("篩選").fill(`CID-${tag}`);
    await expect(pane.locator("tbody tr", { hasText: `CID-${tag}` })).toBeVisible({ timeout: 15_000 });
    await pane.getByRole("button", { name: "匯出" }).click();
    const [dl] = await Promise.all([page.waitForEvent("download"), page.getByText("CSV", { exact: true }).click()]);
    const csv = fs.readFileSync(await dl.path() as string, "utf8");
    expect(csv).toContain(`Hinet-${tag}`);
    expect(csv).toContain(`DSL / VDSL ${tag}`);
    expect(csv).toContain("使用中");
    expect(csv).not.toContain(provider.id);
    expect(csv).not.toContain(ctype.id);
    expect(csv).not.toMatch(/"active"/);
  } finally {
    await request.delete(`/api/v1/circuits/${circuit.id}`, { headers: auth });
    await request.delete(`/api/v1/circuit-types/${ctype.id}`, { headers: auth });
    await request.delete(`/api/v1/providers/${provider.id}`, { headers: auth });
  }
});
