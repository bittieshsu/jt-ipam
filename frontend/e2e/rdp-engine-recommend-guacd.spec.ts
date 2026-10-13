/**
 * 系統設定的 RDP／VNC 引擎：一律建議 guacd（使用者 2026-10-13）。
 * 客戶看到「缺 FreeRDP 套件」的黃色警告（以前不管選哪個引擎都顯示），以為要裝 FreeRDP 就切了過去。
 *   - 用 guacd 時：這台沒有 FreeRDP 也不提
 *   - 選了 FreeRDP 卻沒裝：紅色錯誤＋建議改回 guacd，安裝指令只當「仍要用 FreeRDP 的話」
 *   - 選了備用引擎（FreeRDP／aardwolf、VNC 內建）：提示建議 guacd，按一下就改回去
 * 攔截設定 API，不動共用的全站設定。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

function console_(over: Record<string, unknown>) {
  return {
    rdp_clipboard_paste: false, console_relay: false, sftp_max_file_mb: 2048,
    rdp_engine: "guacd", vnc_engine: "guacd", ssh_engine: "builtin",
    freerdp_available: false, freerdp_missing: ["xfreerdp", "Xvfb"], freerdp_install_cmd: "apt-get install -y freerdp3-x11 xvfb",
    aardwolf_available: true, python_version: "3.12",
    guacd_available: true, guacd_protocols: { rdp: true, vnc: true, ssh: true }, guacd_address: "127.0.0.1:4822",
    guacd_error: "", guacd_install_cmd: "",
    ...over,
  };
}

async function open(page: Page, over: Record<string, unknown>) {
  let state = console_(over);
  const puts: any[] = [];
  await page.route("**/api/v1/system/console-security", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      puts.push(body);
      state = { ...state, ...body };
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(state) });
  });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/system-settings");
  await expect(page.getByTestId("rdp-engine-row")).toBeVisible({ timeout: 20_000 });
  return puts;
}

test("用 guacd 時，沒有 FreeRDP 也不提", async ({ page }) => {
  await open(page, {});
  await expect(page.getByTestId("rdp-freerdp-missing")).toHaveCount(0);
  await expect(page.getByTestId("rdp-engine-recommend")).toHaveCount(0);
  await expect(page.getByTestId("vnc-engine-recommend")).toHaveCount(0);
});

test("選了 FreeRDP 卻沒裝：紅色錯誤、建議改回 guacd，按一下就改回去", async ({ page }) => {
  const puts = await open(page, { rdp_engine: "freerdp" });
  const missing = page.getByTestId("rdp-freerdp-missing");
  await expect(missing).toBeVisible();
  // 錯誤色（不是黃色警告）：Naive UI 不把類型寫進 class，看它套用的圖示顏色
  const iconColor = await missing.evaluate((el) => getComputedStyle(el).getPropertyValue("--n-icon-color").trim());
  const [r, g] = (iconColor.match(/\d+/g) ?? []).map(Number);
  expect(r, iconColor).toBeGreaterThan(150);       // 紅
  expect(g, iconColor).toBeLessThan(140);          // 警告的黃色 G 值很高，分得開
  await expect(missing).toContainText("guacd");
  const rec = page.getByTestId("rdp-engine-recommend");
  await expect(rec).toBeVisible();
  const btn = rec.getByRole("button", { name: /改用 guacd/ });
  await expect(btn.locator("svg")).toHaveCount(1);
  await btn.click();
  await expect.poll(() => puts.map((p) => p.rdp_engine)).toContain("guacd");
  await expect(page.getByTestId("rdp-engine-recommend")).toHaveCount(0);
  await expect(page.getByTestId("rdp-freerdp-missing")).toHaveCount(0);
});

test("aardwolf、VNC 內建：都建議改用 guacd，aardwolf 標明將淘汰", async ({ page }) => {
  const puts = await open(page, { rdp_engine: "aardwolf", vnc_engine: "builtin" });
  await expect(page.getByTestId("rdp-engine-recommend")).toContainText("淘汰");
  await expect(page.getByTestId("rdp-freerdp-missing")).toHaveCount(0);
  const vnc = page.getByTestId("vnc-engine-recommend");
  await expect(vnc).toBeVisible();
  await vnc.getByRole("button", { name: /改用 guacd/ }).click();
  await expect.poll(() => puts.map((p) => p.vnc_engine)).toContain("guacd");
  await expect(page.getByTestId("vnc-engine-recommend")).toHaveCount(0);
});
