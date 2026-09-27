/**
 * AI 助手的浮動按鈕不可以蓋住彈出層（確認框、下拉選單、對話框）。
 *   0.6.50 發版前 e2e 抓到：位址範圍的「刪除」確認框剛好開在畫面右下角，
 *   「確定」有一半壓在浮動按鈕底下 —— 點下去開的是 AI 助手，範圍刪不掉。
 *   浮動按鈕原本 z-index 9000，比 Naive UI 彈出層（2000 起跳）還高。
 * 不靠「剛好開在哪裡」：把一個真的下拉選單搬到浮動按鈕正上方，看最上層是誰。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS env 才能跑");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username|ユーザー名/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password|パスワード/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /^(登入|Sign in|サインイン)$/ }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

test("彈出層蓋在 AI 助手浮動按鈕上面", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await login(page);
  await page.goto("/");
  const fab = page.locator(".chat-fab");
  await expect(fab).toBeVisible();

  // 右上角的帳號選單：一個真的 Naive UI 下拉（掛在 body 底下的 follower 容器）
  await page.getByText(/admin@/).first().click();
  const menu = page.locator(".n-dropdown-menu").first();
  await expect(menu).toBeVisible();

  const top = await page.evaluate(() => {
    const fabEl = document.querySelector(".chat-fab") as HTMLElement;
    const menuEl = document.querySelector(".n-dropdown-menu") as HTMLElement;
    const follower = menuEl.closest(".v-binder-follower-content") as HTMLElement;
    const r = fabEl.getBoundingClientRect();
    // 把下拉整個搬到浮動按鈕正上方（只改位置，不動任何層級）
    follower.style.transform = "none";
    follower.style.position = "fixed";
    follower.style.left = `${r.left - 20}px`;
    follower.style.top = `${r.top - 20}px`;
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {
      inMenu: !!hit && menuEl.contains(hit),
      inFab: !!hit && fabEl.contains(hit),
    };
  });
  expect(top.inFab, "浮動按鈕蓋在下拉選單上面").toBe(false);
  expect(top.inMenu).toBe(true);
});
