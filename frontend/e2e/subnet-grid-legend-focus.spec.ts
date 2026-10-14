/**
 * 指示計的圖例可以點（使用者 2026-10-13）：點一個類型，其他類型的圖例與格子變很淡；再點一次恢復。
 * 鍵盤也能操作（Tab 到圖例、Enter／空白鍵），選中的那一個有明顯的選取狀態（aria-pressed）——
 * 否則使用者忘了自己點過，會以為格子壞了。
 * 樣本：seed_e2e 的 10.20.0.0/24（有已登記的位址，也有 10.20.0.240／241 兩個未納管）。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
}

async function openSubnet(page: Page) {
  const subs = await page.evaluate(async () => {
    const r = await fetch("/api/v1/subnets?page_size=500", {
      headers: { Authorization: `Bearer ${sessionStorage.getItem("access_token")}` } });
    return r.json();
  });
  const sn = subs.items.find((s: any) => s.cidr === "10.20.0.0/24");
  await page.goto(`/subnets/${sn.id}`);
  const grid = page.getByTestId("subnet-grid");
  await expect(grid.locator(".cell").first()).toBeVisible({ timeout: 20_000 });
  return grid;
}

test("點圖例只看那一類：其他格子與圖例變淡，再點一次恢復", async ({ page }) => {
  await login(page);
  const grid = await openSubnet(page);
  const um = grid.getByTestId("grid-legend-unmanaged");
  await expect(um).toHaveAttribute("aria-pressed", "false");
  await um.click();
  await expect(um).toHaveAttribute("aria-pressed", "true");

  const umCells = grid.locator(".cell.cell-unmanaged");
  const others = grid.locator(".cell:not(.cell-unmanaged)");
  expect(await umCells.count()).toBeGreaterThan(0);
  await expect(umCells.first()).not.toHaveClass(/cell-dim/);
  await expect(others.first()).toHaveClass(/cell-dim/);
  expect(await grid.locator(".cell.cell-dim").count()).toBe(await others.count());
  // 其他圖例也變淡
  const online = grid.locator(".legend-item[data-legend='online']");
  await expect(online).toHaveClass(/legend-dim/);
  await expect(um).not.toHaveClass(/legend-dim/);

  await um.click();
  await expect(um).toHaveAttribute("aria-pressed", "false");
  await expect(grid.locator(".cell.cell-dim")).toHaveCount(0);
  await expect(online).not.toHaveClass(/legend-dim/);
});

test("換點另一個類型直接切換；鍵盤 Enter 也能點；Esc 恢復；沒變淡的格子數＝圖例上的數字", async ({ page }) => {
  await login(page);
  const grid = await openSubnet(page);
  const free = grid.locator(".legend-item[data-legend='free']");
  await free.click();
  await expect(free).toHaveAttribute("aria-pressed", "true");

  // 挑一個有位址的狀態類型（樣本裡哪一類有資料看時間而定，不寫死「上線」）
  let pick: { key: string; n: number } | null = null;
  for (const key of ["online", "offline", "unknown", "stale", "reserved"]) {
    const txt = await grid.locator(`.legend-item[data-legend='${key}']`).innerText();
    const n = Number(/\((\d+)\)/.exec(txt)?.[1] ?? 0);
    if (n > 0) { pick = { key, n }; break; }
  }
  expect(pick, "樣本子網路至少要有一類有位址").not.toBeNull();
  const item = grid.locator(`.legend-item[data-legend='${pick!.key}']`);
  await item.focus();
  await page.keyboard.press("Enter");
  await expect(item).toHaveAttribute("aria-pressed", "true");
  await expect(free).toHaveAttribute("aria-pressed", "false");
  const total = await grid.locator(".cell").count();
  // 點選的分類規則跟圖例的計數是同一套：沒變淡的就是圖例上那個數字
  await expect(grid.locator(".cell.cell-dim")).toHaveCount(total - pick!.n);

  await page.keyboard.press("Escape");
  await expect(grid.locator(".cell.cell-dim")).toHaveCount(0);
  await expect(item).toHaveAttribute("aria-pressed", "false");
});

test("只看閒置：閒置格要明顯（實線框＋底色），其他格子變成灰階的極淡", async ({ page }) => {
  // 使用者 2026-10-13：以前閒置格是透明底＋灰色虛線，其他類型雖然變淡仍是淺色塊，閒置格反而不突出
  await login(page);
  const grid = await openSubnet(page);
  await grid.locator(".legend-item[data-legend='free']").click();
  const freeCells = grid.locator(".cell.cell-free.cell-focus");
  expect(await freeCells.count()).toBeGreaterThan(0);
  const focus = await freeCells.first().evaluate((el) => {
    const s = getComputedStyle(el);
    return { border: s.borderTopStyle, bg: s.backgroundColor };
  });
  expect(focus.border).toBe("solid");
  expect(focus.bg).not.toBe("rgba(0, 0, 0, 0)");
  const dim = await grid.locator(".cell.cell-dim").first().evaluate((el) => getComputedStyle(el).filter);
  expect(dim).toContain("grayscale");
  // 再點一次恢復：沒有強調、沒有淡化
  await grid.locator(".legend-item[data-legend='free']").click();
  await expect(grid.locator(".cell.cell-focus")).toHaveCount(0);
  await expect(grid.locator(".cell.cell-dim")).toHaveCount(0);
});
