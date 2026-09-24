/**
 * 「未裝 Agent 的 IP」可以依區段／子網路／單位篩選 —— Wazuh 與 OCS 兩頁都要（共用元件）。
 * 另外：OCS 代理版本顯示精簡版（Unix 2.10.0），原始長字串會把版本號截掉。
 */
import { test, expect } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function login(page: any) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
}

for (const path of ["/wazuh", "/ocs"]) {
  test(`${path}：未裝 Agent 的 IP 依區段篩選`, async ({ page }) => {
    await login(page);
    await page.goto(path);
    await page.locator(".n-tabs-tab", { hasText: /未裝 Agent 的 IP/ }).click();
    const pane = page.locator(".n-tab-pane:visible");
    const alert = pane.locator(".n-alert").first();
    await expect(alert).toBeVisible({ timeout: 20_000 });
    const before = (await alert.innerText()).trim();

    // 選第一個區段
    await pane.locator(".n-base-selection", { hasText: "區段" }).click();
    const opt = page.locator(".n-base-select-option").first();
    const section = (await opt.innerText()).trim();
    await opt.click();
    await page.waitForTimeout(400);

    await expect(alert).toHaveText(/\d+ \/ \d+/);             // 篩選中：「篩選數 / 總數」
    expect((await alert.innerText()).trim()).not.toBe(before);
    const cells = await pane.locator(".n-data-table-tbody .n-data-table-tr").evaluateAll(
      (trs: Element[], idx: number) => trs.map((tr) => (tr.children[idx] as HTMLElement)?.innerText.trim()),
      await pane.locator(".n-data-table-th").evaluateAll((ths: Element[]) =>
        ths.findIndex((th) => (th as HTMLElement).innerText.trim().startsWith("區段"))));
    expect(cells.length).toBeGreaterThan(0);
    for (const c of cells) expect(c).toBe(section);
  });
}

test("OCS 代理版本顯示精簡版", async ({ page }) => {
  await login(page);
  await page.goto("/ocs");
  await page.locator(".n-tabs-tab", { hasText: /代理數/ }).click();
  const cell = page.locator(".n-data-table-tr", { hasText: "101" }).locator("span[title^='OCS-NG_']");
  await expect(cell).toHaveText("Unix 2.10.0", { timeout: 20_000 });
});

test("三個篩選下拉排在同一列（不是上下疊）", async ({ page }) => {
  await login(page);
  await page.goto("/wazuh");
  await page.locator(".n-tabs-tab", { hasText: /未裝 Agent 的 IP/ }).click();
  const sels = page.locator(".n-tab-pane:visible .scope-filter .n-base-selection");
  await expect(sels).toHaveCount(3, { timeout: 20_000 });
  const ys = await sels.evaluateAll((els: Element[]) => els.map((e) => Math.round(e.getBoundingClientRect().top)));
  expect(new Set(ys).size, `三個下拉的 top：${ys.join(", ")}`).toBe(1);
});
