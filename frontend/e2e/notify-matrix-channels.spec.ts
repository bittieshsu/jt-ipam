import { test, expect } from "@playwright/test";

/**
 * 通知矩陣逐一勾選通訊軟體（10-25 使用者）：有啟用的通訊軟體才出現一欄；Email 與每個通訊軟體欄都有「同通知」，
 * 按下去照著第一欄（通知）勾。API 用攔截的固定內容，不改共用的系統設定。
 */
const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

test("只有啟用的通訊軟體出現在矩陣；「同通知」照第一欄勾，存檔送出各欄的值", async ({ page }) => {
  await page.route(/\/api\/v1\/system\/notification-channels$/, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const cfg = await (await route.fetch()).json();
    await route.fulfill({ json: { ...cfg, zulip_enabled: true, telegram_enabled: true, slack_enabled: false,
                                  teams_enabled: false, nextcloud_enabled: false, webhook_enabled: false } });
  });
  const EV = ["ip_request.created", "anomaly.arp_flux"];
  let sent: Record<string, Record<string, boolean>> | null = null;
  await page.route(/\/api\/v1\/system\/notification-matrix$/, async (route) => {
    const channels = ["telegram", "slack", "teams", "nextcloud", "zulip", "webhook"];
    const row = (inApp: boolean, im: boolean) => ({ in_app: inApp, email: false,
      ...Object.fromEntries(channels.map((c) => [c, im])) });
    if (route.request().method() === "PUT") {
      sent = route.request().postDataJSON().matrix;
      return route.fulfill({ json: { matrix: sent, events: EV, channels } });
    }
    // 第一列通知開、第二列關；通訊軟體欄故意都跟通知相反，看「同通知」有沒有照著勾
    await route.fulfill({ json: { matrix: { [EV[0]]: row(true, false), [EV[1]]: row(false, true) }, events: EV, channels } });
  });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await page.goto("/notification-channels");

  await expect(page.getByTestId("nmx-col-zulip")).toContainText("Zulip");
  await expect(page.getByTestId("nmx-col-telegram")).toContainText("Telegram");
  await expect(page.getByTestId("nmx-col-slack")).toHaveCount(0);
  for (const col of ["email", "telegram", "zulip"]) await expect(page.getByTestId(`nmx-copy-${col}`)).toBeVisible();

  const box = (col: string, ev: string) => page.getByTestId(`nmx-${col}-${ev}`);
  await expect(box("zulip", EV[0])).not.toHaveClass(/n-checkbox--checked/);
  await page.getByTestId("nmx-copy-zulip").click();
  await expect(box("zulip", EV[0])).toHaveClass(/n-checkbox--checked/);
  await expect(box("zulip", EV[1])).not.toHaveClass(/n-checkbox--checked/);
  // Telegram 沒按，維持原樣
  await expect(box("telegram", EV[1])).toHaveClass(/n-checkbox--checked/);

  await page.locator(".n-card", { hasText: "通知矩陣" }).getByRole("button", { name: /儲存/ }).click();
  await expect.poll(() => sent?.[EV[0]]?.zulip).toBe(true);
  expect(sent![EV[1]].zulip).toBe(false);
  expect(sent![EV[1]].telegram).toBe(true);
});
