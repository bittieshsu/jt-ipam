import { test, expect, type Page } from "@playwright/test";

/**
 * SSH 連到 Linux 時提示搭配 tmux／screen（10-22 使用者：「如果是 ssh 且是 linux，上面可以有個訊息，
 * 建議搭配 tmux 或 screen 確保連線上的工作不因斷線而被回收」）。
 * 只在看得出是 Linux 時顯示；按 × 之後這個瀏覽器不再顯示。IP 資料用攔截的，不需要真的 SSH 主機。
 */
const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

const ID = "00000000-0000-4000-8000-0000000000aa";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

async function fakeAddress(page: Page, os: { os_family: string | null; os_guess: string | null }) {
  await page.route(new RegExp(`/api/v1/addresses/${ID}$`), (r) => r.fulfill({ json: {
    id: ID, ip: "192.0.2.50", hostname: "app-01", device_name: null, ssh_enabled: true, ...os } }));
}

test("Linux 顯示 tmux／screen 提示，關掉後不再顯示；Windows 不顯示", async ({ page }) => {
  await login(page);
  await fakeAddress(page, { os_family: "linux", os_guess: "Ubuntu 24.04" });
  await page.goto(`/ssh/${ID}`);
  const hint = page.getByTestId("ssh-tmux-hint");
  await expect(hint).toBeVisible({ timeout: 15_000 });
  await expect(hint).toContainText("tmux");
  await expect(hint).toContainText("screen");
  await hint.locator(".n-base-close").click();
  await expect(hint).toHaveCount(0);
  await page.reload();
  await expect(page.locator('input[placeholder="root"]')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("ssh-tmux-hint")).toHaveCount(0);

  // 新的瀏覽器狀態、Windows：不顯示
  await page.evaluate(() => localStorage.removeItem("jtipam.ssh.tmuxHintDismissed"));
  await page.unroute(new RegExp(`/api/v1/addresses/${ID}$`));
  await fakeAddress(page, { os_family: "windows", os_guess: "Windows Server 2022" });
  await page.reload();
  await expect(page.locator('input[placeholder="root"]')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("ssh-tmux-hint")).toHaveCount(0);
});
