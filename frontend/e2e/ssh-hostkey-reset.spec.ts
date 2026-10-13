import { test, expect, type Page } from "@playwright/test";

/**
 * SSH 主機金鑰不符時的重設路徑（2026-10-12，殘留資料全面檢查）。
 *
 * 位址換給另一台設備之後，記住的金鑰一定對不上，主控台只會一直說「可能遭中間人攻擊」。
 * 這裡驗：錯誤訊息下面出現「清除記住的主機金鑰」→ 確認後清掉 → 再連一次會跳出新金鑰的指紋（不是直接信任）。
 *
 * 需要一筆 ssh_enabled 的 IP，記住的金鑰跟主機現在的金鑰不同（例如把 127.0.0.1 那筆的
 * ssh_host_key 改成別把金鑰）：
 *   E2E_SSH_MISMATCH_ADDRESS_ID=<那筆 IP 的 id>
 * 金鑰不符在交換金鑰時就擋下，帳號隨便填、不需要密碼。
 *
 * SFTP 跟 SSH 共用同一把釘選：以前 SFTP 完全不驗主機金鑰（帳密會交給冒充的主機），
 * 現在沒釘選時一樣先跳出指紋確認、不符時一樣擋下並提供清除。
 * 兩個測試都會改動釘選（清掉），所以照順序跑：先 SSH（不符→清除），再 SFTP（沒釘選→確認）。
 */
const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const ADDRESS_ID = process.env.E2E_SSH_MISMATCH_ADDRESS_ID || "";

test.skip(!ADMIN_PASS || !ADDRESS_ID, "需要 E2E_ADMIN_PASS + E2E_SSH_MISMATCH_ADDRESS_ID 才能跑");

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

async function connect(page: Page) {
  const user = page.locator('input[placeholder="root"]');
  await expect(user).toBeVisible({ timeout: 15_000 });
  await user.fill("nobody");
  await page.getByRole("button", { name: /SSH 連線|Connect/ }).first().click();
}

test.describe.configure({ mode: "serial" });

test("金鑰不符：可以清除記住的金鑰，重連時要再核對新指紋", async ({ page }) => {
  await login(page);
  await page.goto(`/ssh/${ADDRESS_ID}`);
  await connect(page);

  const box = page.getByTestId("ssh-hostkey-reset");
  await expect(box).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/主機金鑰與先前釘選不符/)).toBeVisible();
  await expect(box).toContainText("換給另一台設備");

  await box.getByRole("button", { name: "清除記住的主機金鑰" }).click();
  await page.locator(".n-popconfirm").getByRole("button", { name: /確認|確定|OK/ }).click();
  await expect(page.locator(".n-message")).toContainText("已清除", { timeout: 10_000 });

  // 回到連線表單；再連一次 → 跳出指紋確認（清掉之後不是自動信任）
  await connect(page);
  const dlg = page.getByRole("dialog").filter({ hasText: "確認主機金鑰" });
  await expect(dlg).toBeVisible({ timeout: 20_000 });
  await expect(dlg).toContainText("SHA256:");
  await dlg.getByRole("button", { name: /取消|Cancel/ }).click();
});

test("SFTP：沒有記住的金鑰時先核對指紋，取消就不連", async ({ page }) => {
  // 上一個測試已經把釘選清掉
  await login(page);
  await page.goto(`/sftp/${ADDRESS_ID}`);
  const user = page.locator('input[placeholder="root"]');
  await expect(user).toBeVisible({ timeout: 15_000 });
  await user.fill("nobody");
  await page.getByRole("button", { name: /^連線$|Connect/ }).first().click();

  const dlg = page.getByRole("dialog").filter({ hasText: "確認主機金鑰" });
  await expect(dlg).toBeVisible({ timeout: 20_000 });
  await expect(dlg).toContainText("SHA256:");
  await dlg.getByRole("button", { name: /取消|Cancel/ }).click();
  await expect(page.getByText("已取消（未信任主機金鑰）")).toBeVisible({ timeout: 10_000 });
});
