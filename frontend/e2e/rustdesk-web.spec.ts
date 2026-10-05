import { test, expect, type Page } from "@playwright/test";

/**
 * 相容 RustDesk 的網頁連線：對著測試靶（scripts/rustdesk-test-target）連一次，確認登入、出畫面、打字；
 * 第二支驗「記住密碼」（附錄 D）：登入成功後才存、再開啟不必輸入、已存的失效時可以刪除並重新記住。
 *
 * 需要四樣東西，缺一就跳過：
 *   E2E_ADMIN_PASS      管理員密碼（E2E_ADMIN_USER 預設 admin）
 *   E2E_RD_IP_ID        已用 seed_jtipam.py 接好的那筆 IP 記錄 id（198.51.100.77）
 *   E2E_RD_PASSWORD     受控端的永久密碼（測試靶是 Rd-Test-2026）
 *   瀏覽器要支援 WebCodecs 的 VP9（Chromium 都有）
 *
 * ⚠️ 本機驗證時前端要用同源方式跑（`vite preview` 的 proxy 指到後端），WebSocket 網址是用 window.location 組的。
 * ⚠️ 兩支各會故意錯一次密碼：同一個帳號對同一台一分鐘最多錯 3 次（7.8 的限流），整個檔案跑完要隔一分鐘才能再跑。
 */
const PASS = process.env.E2E_ADMIN_PASS || "";
const IPID = process.env.E2E_RD_IP_ID || "";
const RDPW = process.env.E2E_RD_PASSWORD || "";

test.skip(!PASS || !IPID || !RDPW, "需要 E2E_ADMIN_PASS、E2E_RD_IP_ID 與 E2E_RD_PASSWORD（見檔頭）");

test("相容 RustDesk 的網頁連線：密碼錯可以重輸、連上後出畫面、打得了字", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 950 });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(process.env.E2E_ADMIN_USER || "admin");
  await page.getByPlaceholder(/密碼|Password/).fill(PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await page.waitForURL((u) => !u.pathname.includes("/login"));

  await page.goto(`/rustdesk/${IPID}`);
  await page.getByTestId("rdweb-password").locator("input").fill("definitely-wrong");
  await page.getByTestId("rdweb-connect").click();

  // 7.5：密碼錯不會斷線，沿用同一個 Hash 重送
  await expect(page.getByTestId("rdweb-password-prompt")).toBeVisible({ timeout: 40_000 });
  await page.getByTestId("rdweb-retry-password").locator("input").fill(RDPW);
  await page.getByTestId("rdweb-retry-submit").click();

  const canvas = page.getByTestId("rdweb-canvas");
  await expect(canvas).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("rdweb-status")).toContainText("已連線");
  await page.waitForTimeout(1500);

  // 「有 canvas」不等於「畫出來了」：量實際像素
  const painted = await canvas.evaluate((c: HTMLCanvasElement) => {
    const ctx = c.getContext("2d")!;
    const d = ctx.getImageData(0, 0, Math.min(c.width, 400), Math.min(c.height, 300)).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i] || d[i + 1] || d[i + 2]) n++;
    return { w: c.width, h: c.height, n };
  });
  expect(painted.w, "canvas 尺寸來自解出來的影格").toBeGreaterThan(100);
  expect(painted.n, "canvas 全黑＝沒有解出畫面").toBeGreaterThan(500);

  // 鍵盤：點進畫面、打一行字（測試靶的桌面是一個 xterm）
  const box = (await canvas.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.keyboard.type("echo jt-ipam-web-e2e", { delay: 25 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "test-results/rustdesk-web.png" });

  await page.getByTestId("rdweb-disconnect").click();
  await expect(page.getByTestId("rdweb-status")).toContainText("已關閉");
});

// ── 記住密碼（附錄 D）──

type Cred = { id: string; target_ip_id: string | null };

async function api<T>(page: Page, method: string, path: string, body?: unknown): Promise<T> {
  return page.evaluate(async ({ method, path, body }) => {
    const r = await fetch(path, {
      method,
      headers: { Authorization: `Bearer ${localStorage.getItem("access_token") || ""}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return (r.status === 204 ? null : await r.json()) as T;
  }, { method, path, body });
}

const savedHere = async (page: Page) =>
  (await api<Cred[]>(page, "GET", `/api/v1/ssh-credentials?protocol=rustdesk&target_ip_id=${IPID}`))
    .filter((c) => c.target_ip_id === IPID);

async function removeSaved(page: Page) {
  for (const c of await savedHere(page)) await api(page, "DELETE", `/api/v1/ssh-credentials/${c.id}`);
}

async function connectedOrFail(page: Page) {
  await expect(page.getByTestId("rdweb-status")).toContainText("已連線", { timeout: 40_000 });
}

test("記住密碼：登入成功後才存、再開啟不必輸入、失效時可以刪除並重新記住；瀏覽器不存密碼", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 950 });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(process.env.E2E_ADMIN_USER || "admin");
  await page.getByPlaceholder(/密碼|Password/).fill(PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await page.waitForURL((u) => !u.pathname.includes("/login"));
  await removeSaved(page);            // 從乾淨的狀態開始（上一次中斷時可能留下）
  try {
    // 1) 勾「記住密碼」、登入成功 → 金庫多一筆 rustdesk
    await page.goto(`/rustdesk/${IPID}`);
    await page.getByTestId("rdweb-password").locator("input").fill(RDPW);
    await page.getByTestId("rdweb-remember").click();
    await page.getByTestId("rdweb-connect").click();
    await connectedOrFail(page);
    await expect.poll(async () => (await savedHere(page)).length, { timeout: 10_000 }).toBe(1);
    const storage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
    expect(storage, "瀏覽器的儲存空間不放密碼").not.toContain(RDPW);
    await page.getByTestId("rdweb-disconnect").click();

    // 2) 再開啟：「已存密碼」下拉預設選剛存的那筆（同 VNC），不顯示輸入框，直接連上
    await page.goto(`/rustdesk/${IPID}`);
    await expect(page.getByTestId("rdweb-saved-password")).toContainText("RustDesk", { timeout: 15_000 });
    await expect(page.getByTestId("rdweb-password")).toHaveCount(0);
    await page.getByTestId("rdweb-connect").click();
    await connectedOrFail(page);
    await page.getByTestId("rdweb-disconnect").click();

    // 3) 模擬對方改了密碼：把已存的換成錯的 → 顯示已失效、不自動重試；刪掉之後輸入正確的並重新記住
    await api(page, "POST", "/api/v1/ssh-credentials", {
      label: "RustDesk e2e", username: "", auth_type: "password", protocol: "rustdesk",
      target_ip_id: IPID, password: "not-the-password",
    });
    await page.goto(`/rustdesk/${IPID}`);
    await expect(page.getByTestId("rdweb-saved-password")).toContainText("RustDesk e2e", { timeout: 15_000 });
    await page.getByTestId("rdweb-connect").click();
    await expect(page.getByTestId("rdweb-saved-notice")).toContainText("已存的密碼已失效", { timeout: 40_000 });
    await page.getByTestId("rdweb-retry-delete-saved").click();
    await page.locator(".n-popconfirm__action button").last().click();
    await expect.poll(async () => (await savedHere(page)).length, { timeout: 10_000 }).toBe(0);
    await page.getByTestId("rdweb-retry-password").locator("input").fill(RDPW);
    await page.getByTestId("rdweb-retry-remember").click();
    await page.getByTestId("rdweb-retry-submit").click();
    await connectedOrFail(page);
    await expect.poll(async () => (await savedHere(page)).length, { timeout: 10_000 }).toBe(1);
    await page.getByTestId("rdweb-disconnect").click();
  } finally {
    await removeSaved(page);
  }
});

// ── 斷線後自動重新連線（附錄 G）：連上之後把受控端重新啟動 ──
// 需要 E2E_RD_RESTART_CMD（例：`docker restart rdtest-client`）才跑；受控端在 G.1 的情況就是這樣：直接斷、沒有 close_reason
const RESTART = process.env.E2E_RD_RESTART_CMD || "";

test("受控端重新啟動：顯示倒數、自動連回來、看得到畫面（不用手動按重新連線）", async ({ page }) => {
  test.skip(!RESTART, "需要 E2E_RD_RESTART_CMD");
  test.setTimeout(240_000);
  const { execSync } = await import("node:child_process");
  await page.setViewportSize({ width: 1400, height: 950 });
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(process.env.E2E_ADMIN_USER || "admin");
  await page.getByPlaceholder(/密碼|Password/).fill(PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await page.waitForURL((u) => !u.pathname.includes("/login"));
  await removeSaved(page);

  await page.goto(`/rustdesk/${IPID}`);
  await page.getByTestId("rdweb-password").locator("input").fill(RDPW);
  await page.getByTestId("rdweb-connect").click();
  await connectedOrFail(page);

  execSync(RESTART, { stdio: "ignore", timeout: 60_000 });
  // 倒數或嘗試中（不是終止的「連線已中斷」）
  await expect(page.getByText(/秒後自動重新連線|正在重新連線/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("rdweb-reconnect"), "不可以停在要手動按「重新連線」").toHaveCount(0);
  // 受控端回來後自動連上、出畫面
  await expect(page.getByTestId("rdweb-status")).toContainText("已連線", { timeout: 120_000 });
  const canvas = page.getByTestId("rdweb-canvas");
  await page.waitForTimeout(2000);
  const n = await canvas.evaluate((c: HTMLCanvasElement) => {
    const d = c.getContext("2d")!.getImageData(0, 0, Math.min(c.width, 400), Math.min(c.height, 300)).data;
    let k = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i] || d[i + 1] || d[i + 2]) k++;
    return k;
  });
  expect(n, "重連之後要看得到畫面").toBeGreaterThan(500);
  // 瀏覽器儲存空間沒有密碼
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain(RDPW);

  // 取消：再斷一次，按「取消」→ 回到手動重新連線
  execSync(RESTART, { stdio: "ignore", timeout: 60_000 });
  await expect(page.getByTestId("rdweb-reconnect-cancel")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("rdweb-reconnect-cancel").click();
  await expect(page.getByTestId("rdweb-reconnect")).toBeVisible();
});
