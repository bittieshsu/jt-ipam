/**
 * 防火牆規則異動：類型標籤要跟它那一行異動內容對齊，內容換行也一樣（使用者 2026-10-13：
 * 「類型欄位跟異動內容換行就對不上了」）；每一行的文字起點相同、與「異動內容」標題對齊。
 * 清單端點攔截：第一條規則描述很長、一定會換行。
 */
import { test, expect } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

const LONG = "允許分公司到資料中心的備份流量，" .repeat(8);
const ITEMS = [{
  id: "00000000-0000-4000-8000-000000000001", source_type: "opnsense", instance_name: "fw-e2e",
  taken_at: "2026-10-13T08:00:00Z", rule_count: 42, is_baseline: false, ack: null,
  diff: {
    added: [{ action: "pass", src: "198.51.100.0/24", dst: "203.0.113.10", dst_port: "873", descr: LONG }],
    removed: [{ action: "block", src: "any", dst: "203.0.113.20", descr: "old" }],
    changed: [{ key: "r3", descr: "dns", fields: ["dst"], old: { dst: "203.0.113.53" }, new: { dst: "203.0.113.54" } }],
  },
}, {
  id: "00000000-0000-4000-8000-000000000002", source_type: "opnsense", instance_name: "fw-e2e",
  taken_at: "2026-10-12T08:00:00Z", rule_count: 40, is_baseline: true, ack: null, diff: null,
}];

test("類型標籤與異動內容逐行對齊（含換行）", async ({ page }) => {
  await page.route("**/api/v1/anomalies/fw-rule-changes", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: ITEMS }) }));
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/fw-rule-changes");

  const grid = page.getByTestId("fw-change-lines");
  await expect(grid).toHaveCount(1);
  const geo = await grid.evaluate((el) => {
    const kids = Array.from(el.children) as HTMLElement[];
    const pairs = [];
    for (let i = 0; i < kids.length; i += 2) {
      const tag = kids[i].getBoundingClientRect();
      const txt = kids[i + 1].getBoundingClientRect();
      pairs.push({ tagTop: tag.top, txtTop: txt.top, txtLeft: txt.left, txtHeight: txt.height });
    }
    return pairs;
  });
  expect(geo).toHaveLength(3);
  // 第一行真的換行了（否則這個測試測不到東西）
  expect(geo[0].txtHeight).toBeGreaterThan(30);
  for (const g of geo) {
    expect(Math.abs(g.tagTop - g.txtTop)).toBeLessThan(2);
    expect(Math.abs(g.txtLeft - geo[0].txtLeft)).toBeLessThan(1);
  }
  // 換行之後的下一個標籤跟著往下，貼著它自己那一行
  expect(geo[1].tagTop).toBeGreaterThan(geo[0].tagTop + geo[0].txtHeight - 2);

  // 文字起點與「異動內容」標題的文字起點對齊
  const head = await page.locator("th", { hasText: "異動內容" }).evaluate((th) => {
    const r = th.getBoundingClientRect();
    return r.left + parseFloat(getComputedStyle(th).paddingLeft);
  });
  expect(Math.abs(geo[0].txtLeft - head)).toBeLessThan(2);

  // 初次快照照舊：類型欄一個「—」、異動內容寫「初次快照」
  const baseline = page.locator("tbody tr").nth(1);
  await expect(baseline).toContainText("—");
});
