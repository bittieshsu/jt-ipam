import { test, expect, type APIRequestContext, type Browser, type Page } from "@playwright/test";
import crypto from "node:crypto";

/**
 * 合規核對（2026-10-09）補上的安全控制，用真的瀏覽器走一次：
 *   1. MFA 政策「所有人必須使用」→ 沒設定的人登入時先設定、拿到復原碼；復原碼能登入一次、第二次不行
 *   2. 登入中的裝置：「登出其他所有裝置」之後，另一個瀏覽器下一個動作就回到登入頁
 *   3. Graylog DSV：明文 8088 開關與警告、按「顯示」才看得到權杖
 *   4. 每日備份加密卡片：設定、狀態、移除
 *   5. 開著的主控台被收回權限時，畫面寫出原因（要 E2E_VNC_IP_ID 與 VNC 測試靶，見 vnc-console.spec.ts）
 *
 * 會改全站設定（MFA 政策、DSV、備份加密）→ 列在 global-state-specs.txt，發版時單一 worker 依序跑。
 * ⚠️ 政策設成「所有人必須」之後，連 admin 用 API 登入都拿不到權杖：還原政策用的是**一開始就拿好**的權杖。
 */
const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const VNC_IP_ID = process.env.E2E_VNC_IP_ID || "";
const VNC_PORT = process.env.E2E_VNC_PORT || "5999";

test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS env 才能跑");
test.describe.configure({ mode: "serial" });

// RFC 6238（與 totp.spec.ts 相同；後端 pyotp 預設 SHA1 / 6 碼 / 30 秒）
function base32Decode(s: string): Buffer {
  const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  const out: number[] = [];
  for (const c of s.replace(/=+$/, "").toUpperCase()) {
    const v = A.indexOf(c);
    if (v >= 0) bits += v.toString(2).padStart(5, "0");
  }
  for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(out);
}
function totp(secret: string, atMs = Date.now()): string {
  let counter = Math.floor(atMs / 1000 / 30);
  const buf = Buffer.alloc(8);
  for (let i = 7; i >= 0; i--) { buf[i] = counter & 0xff; counter = Math.floor(counter / 256); }
  const h = crypto.createHmac("sha1", base32Decode(secret)).update(buf).digest();
  const o = h[h.length - 1] & 0xf;
  const code = ((h[o] & 0x7f) << 24) | ((h[o + 1] & 0xff) << 16) | ((h[o + 2] & 0xff) << 8) | (h[o + 3] & 0xff);
  return (code % 1_000_000).toString().padStart(6, "0");
}

let adminAuth: Record<string, string> = {};
const created: string[] = [];

async function newUser(request: APIRequestContext, prefix: string, isAdmin = false) {
  const username = `e2e-${prefix}-${Date.now().toString(36)}`;
  const password = `Sec-${crypto.randomBytes(9).toString("base64url")}`;
  const r = await request.post("/api/v1/users", { headers: adminAuth,
    data: { username, email: `${username}@example.com`, password, is_admin: isAdmin } });
  expect(r.status(), await r.text()).toBe(201);
  const id = (await r.json()).id as string;
  created.push(id);
  return { id, username, password };
}

async function fillLogin(page: Page, username: string, password: string) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(username);
  await page.getByPlaceholder(/密碼|Password/).fill(password);
  await page.getByRole("button", { name: "登入", exact: true }).click();
}

async function freshPage(browser: Browser): Promise<Page> {
  return (await browser.newContext()).newPage();
}

async function setPolicy(request: APIRequestContext, mfa_required: "off" | "admins" | "all") {
  const r = await request.put("/api/v1/system/auth-policy", { headers: adminAuth,
    data: { mfa_required, mfa_apply_to_sso: false } });
  expect(r.ok(), await r.text()).toBeTruthy();
}

test.beforeAll(async ({ request }) => {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok(), "admin API 登入要成功").toBeTruthy();
  adminAuth = { Authorization: `Bearer ${(await r.json()).access_token}` };
});

test.afterAll(async ({ request }) => {
  await setPolicy(request, "off");           // 先還原政策，否則之後的 spec 全部卡在設定 MFA
  for (const id of created) await request.delete(`/api/v1/users/${id}`, { headers: adminAuth });
});

test("MFA 政策：登入時強制設定、拿到復原碼，復原碼只能用一次", async ({ page, browser, request }) => {
  const u = await newUser(request, "mfa");
  // 管理員在使用者頁的「登入安全」設成所有人必須使用（走畫面）
  await fillLogin(page, ADMIN_USER, ADMIN_PASS);
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await page.goto("/users");
  await page.getByTestId("mfa-policy-open").click();
  await page.getByTestId("mfa-policy-select").click();
  await page.locator(".n-base-select-option", { hasText: "所有人必須使用" }).click();
  await page.getByTestId("mfa-policy-save").click();
  await expect.poll(async () => (await (await request.get("/api/v1/system/auth-policy", { headers: adminAuth })).json()).mfa_required)
    .toBe("all");

  // 沒設定的人：輸入密碼之後先設定，不是被擋在外面
  const p = await freshPage(browser);
  await fillLogin(p, u.username, u.password);
  await expect(p.getByTestId("login-mfa-setup")).toBeVisible({ timeout: 15_000 });
  const secret = (await p.getByTestId("totp-secret").textContent())?.trim() || "";
  expect(secret).toMatch(/^[A-Z2-7]+=*$/);
  if (30 - (Math.floor(Date.now() / 1000) % 30) < 3) await p.waitForTimeout(3500);
  await p.getByTestId("totp-enroll-code").locator("input").fill(totp(secret));
  await p.getByTestId("totp-enroll-confirm").click();
  const box = p.getByTestId("recovery-codes");
  await expect(box).toBeVisible({ timeout: 10_000 });
  const codes = (await box.locator("li code").allTextContents()).map((c) => c.trim());
  expect(codes).toHaveLength(10);
  await expect(p.getByTestId("recovery-done")).toBeDisabled();
  await p.getByTestId("recovery-saved").click();
  await p.getByTestId("recovery-done").click();
  await expect(p).not.toHaveURL(/\/login/, { timeout: 15_000 });

  // 復原碼登入一次可以
  const p2 = await freshPage(browser);
  await fillLogin(p2, u.username, u.password);
  await p2.getByTestId("login-mfa-code").locator("input").fill(codes[0]);
  await p2.getByTestId("login-mfa-submit").click();
  await expect(p2).not.toHaveURL(/\/login/, { timeout: 15_000 });

  // 同一組第二次不行
  const p3 = await freshPage(browser);
  await fillLogin(p3, u.username, u.password);
  await p3.getByTestId("login-mfa-code").locator("input").fill(codes[0]);
  await p3.getByTestId("login-mfa-submit").click();
  await p3.waitForTimeout(1500);
  await expect(p3).toHaveURL(/\/login/);
  await expect(p3.getByTestId("login-mfa-code")).toBeVisible();

  await setPolicy(request, "off");
});

test("登入中的裝置：登出其他所有裝置後，另一個瀏覽器回到登入頁", async ({ browser, request }) => {
  const u = await newUser(request, "sess");
  const a = await freshPage(browser);
  const b = await freshPage(browser);
  for (const p of [a, b]) {
    await fillLogin(p, u.username, u.password);
    await expect(p).not.toHaveURL(/\/login/, { timeout: 15_000 });
  }
  await a.goto("/settings");
  await a.locator(".n-tabs-tab", { hasText: "安全" }).click();
  const card = a.getByTestId("my-sessions");
  await expect(card).toBeVisible();
  await expect(card.locator("tbody tr")).toHaveCount(2, { timeout: 10_000 });
  await expect(card).toContainText("這個裝置");
  await card.getByRole("button", { name: "登出其他所有裝置" }).click();
  await a.locator(".n-popconfirm").getByRole("button", { name: /確定|確認|OK/ }).click();
  await expect(card.locator("tbody tr")).toHaveCount(1, { timeout: 10_000 });

  // B 的工作階段已撤銷：下一個要打 API 的動作就回到登入頁（不必等 15 分鐘的存取權杖到期）
  await b.goto("/addresses");
  await expect(b).toHaveURL(/\/login/, { timeout: 15_000 });
  // A 不受影響
  await a.reload();
  await expect(a).not.toHaveURL(/\/login/);
});

test("Graylog DSV：明文 8088 開關與警告、按顯示才看得到權杖", async ({ page }) => {
  await fillLogin(page, ADMIN_USER, ADMIN_PASS);
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await page.goto("/graylog-dsv");
  const sw = page.getByTestId("dsv-allow-plain");
  await expect(sw).toBeVisible();
  const wasOn = (await sw.getAttribute("class"))?.includes("n-switch--active") ?? false;
  await sw.click();
  if (wasOn) await expect(page.getByText("關閉時 8088 埠一律回 404")).toBeVisible();
  else await expect(page.getByText("明文 HTTP 會讓路上的每一台設備都看得到權杖", { exact: false })).toBeVisible();
  await sw.click();                                     // 還原
  await expect.poll(async () => (await sw.getAttribute("class"))?.includes("n-switch--active") ?? false).toBe(wasOn);

  const tok = page.getByTestId("dsv-token");
  if (await page.getByTestId("dsv-token-show").count()) {
    const masked = (await tok.textContent()) || "";
    await page.getByTestId("dsv-token-show").click();
    await expect.poll(async () => (await tok.textContent()) || "").not.toBe(masked);
    await expect(page.getByTestId("dsv-token-expiry")).toBeVisible();
  }
});

test("每日備份加密：設定密碼、顯示已加密、移除", async ({ page, request }) => {
  const before = await (await request.get("/api/v1/system/backup-encryption", { headers: adminAuth })).json();
  test.skip(before.enabled, "這台已經設了備份加密密碼：不覆蓋");
  await fillLogin(page, ADMIN_USER, ADMIN_PASS);
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await page.goto("/system-settings");
  const card = page.getByTestId("backup-encryption");
  await card.scrollIntoViewIfNeeded();
  await expect(card).toContainText("沒有加密");
  const pw = `bk-${crypto.randomBytes(12).toString("base64url")}`;
  await card.getByTestId("backup-enc-pw").locator("input").fill(pw);
  await card.getByTestId("backup-enc-pw2").locator("input").fill(pw);
  await card.getByTestId("backup-enc-save").click();
  await expect(card).toContainText("已加密", { timeout: 10_000 });
  await card.getByRole("button", { name: "移除" }).click();
  await page.locator(".n-popconfirm").getByRole("button", { name: /確定|確認|OK/ }).click();
  await expect(card).toContainText("沒有加密", { timeout: 10_000 });
});

test("開著的主控台被收回權限：畫面寫出原因", async ({ browser, request }) => {
  test.skip(!VNC_IP_ID, "需要 E2E_VNC_IP_ID 與 VNC 測試靶（見 vnc-console.spec.ts）");
  test.setTimeout(120_000);
  const u = await newUser(request, "con", true);
  const p = await freshPage(browser);
  await p.setViewportSize({ width: 1280, height: 900 });
  await fillLogin(p, u.username, u.password);
  await expect(p).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await p.goto(`/vnc/${VNC_IP_ID}`);
  await p.getByPlaceholder("VNC 密碼").fill("any-password");
  await p.locator(".n-input-number input").first().fill(VNC_PORT);
  await p.getByRole("button", { name: /VNC 連線/ }).click();
  // canvas 在連上之前就在了，要等狀態變「已連線」（否則下面降權會比連線早，變成在握手時就被拒）
  await expect(p.getByText("已連線", { exact: true })).toBeVisible({ timeout: 30_000 });

  // 降成一般帳號 → 沒有這個 IP 的 VNC 權限；後端每 30 秒重新檢查一次
  const r = await request.patch(`/api/v1/users/${u.id}`, { headers: adminAuth, data: { is_admin: false } });
  expect(r.ok(), await r.text()).toBeTruthy();
  await expect(p.getByText("你對這個 IP 開主控台的權限已被收回")).toBeVisible({ timeout: 50_000 });
});
