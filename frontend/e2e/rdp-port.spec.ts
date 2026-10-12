/**
 * RDP 連線可以改連接埠（使用者 2026-10-11：VNC 可以選連接埠，RDP 沒有）。
 * 改成 3390、按連線，送給後端的連線設定要帶 port: 3390（目標沒有開 3390，連不上沒關係，驗的是設定有送出去）。
 * 需要 seed_e2e 的 console-target（127.0.0.1，RDP 開啟）與可用的 RDP 引擎（guacd）。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function api(page: Page, method: string, path: string) {
  return page.evaluate(async ({ method, path }) => {
    const r = await fetch(path, { method, headers: { Authorization: `Bearer ${sessionStorage.getItem("access_token")}` } });
    return { status: r.status, json: await r.json().catch(() => null) };
  }, { method, path });
}

test("RDP 連線視窗有連接埠，選的值會送到後端", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  const cs = await api(page, "GET", "/api/v1/system/console-security");
  test.skip(cs.json?.rdp_engine === "guacd" && !cs.json?.guacd_available, "本機沒有 guacd");
  const found = await api(page, "GET", "/api/v1/addresses?q=127.0.0.1&page_size=50");
  const ipId = found.json.items.find((x: any) => String(x.ip).split("/")[0] === "127.0.0.1")?.id || "";
  test.skip(!ipId, "沒有 console-target（先跑 seed_e2e）");

  const sent: string[] = [];
  page.on("websocket", (ws) => ws.on("framesent", (f) => { sent.push(String(f.payload)); }));
  await page.goto(`/rdp/${ipId}`);
  const port = page.getByTestId("rdp-port");
  await expect(port).toBeVisible({ timeout: 15_000 });
  await expect(port.locator("input")).toHaveValue("3389");
  await port.locator("input").fill("3390");
  await port.locator("input").blur();
  const user = page.getByPlaceholder("Administrator");
  if (await user.count()) {
    await user.fill("rdpuser");
    await page.locator("input[type=password]").first().fill("TestPw-123");
  }
  await page.locator(".rdp-form").getByRole("button", { name: "RDP 連線" }).click();
  await expect.poll(() => sent.find((m) => m.startsWith("{") && m.includes('"type":"config"')) ?? "",
    { timeout: 20_000 }).not.toBe("");
  const cfg = JSON.parse(sent.find((m) => m.startsWith("{") && m.includes('"type":"config"'))!);
  expect(cfg.port).toBe(3390);
});
