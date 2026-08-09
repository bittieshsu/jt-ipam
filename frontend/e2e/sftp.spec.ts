/**
 * SFTP：用真實瀏覽器連上一台真的 SFTP 伺服器，實際列目錄、下載、上傳。
 *
 * 「檔案有沒有真的傳過去」只有端到端才問得出來 —— 介面顯示「已上傳」而檔案是空的、
 * 或下載下來少了幾個 chunk，兩者在畫面上看起來都一樣正常。
 */
import { test, expect } from "@playwright/test";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const TARGET_IP_ID = process.env.E2E_SFTP_IP_ID || "";
// 這台 SFTP 目標由 e2e/fixtures/sftp-target.py 起（見該檔開頭的用法）
const SFTP_ROOT = process.env.E2E_SFTP_ROOT || "";
const SFTP_USER = process.env.E2E_SFTP_USER || "tester";
const SFTP_PASS = process.env.E2E_SFTP_PASS || "TestPass!2026";
const SFTP_PORT = process.env.E2E_SFTP_PORT || "2222";

test.skip(!ADMIN_PASS || !TARGET_IP_ID || !SFTP_ROOT,
  "需要 E2E_ADMIN_PASS、E2E_SFTP_IP_ID 與 E2E_SFTP_ROOT");
test.setTimeout(180_000);

async function connect(page: any) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));

  await page.goto(`/sftp/${TARGET_IP_ID}`);
  await expect(page.getByText("目標主機")).toBeVisible();
  await page.getByPlaceholder("root").fill(SFTP_USER);
  // 密碼欄（第一個 password 型別的輸入框）
  await page.locator('input[type="password"]').first().fill(SFTP_PASS);
  await page.locator(".n-input-number input").first().fill(SFTP_PORT);
  await page.keyboard.press("Tab");
  await page.getByRole("button", { name: "連線" }).click();
  // 連上後會自動列出根目錄
  await expect(page.getByText("nginx.conf").or(page.getByText("etc"))).toBeVisible({ timeout: 20_000 });
}

test("連線後可以列出遠端目錄", async ({ page }) => {
  await connect(page);
  const body = await page.locator("body").innerText();
  expect(body, "看不到遠端的檔案").toContain("app.log");
  expect(body, "看不到目錄").toContain("etc");
  // 含中文的檔名不可以變成亂碼
  expect(body, "中文檔名壞掉").toContain("readme-中文.txt");
  await page.screenshot({ path: "test-results/sftp-list.png" });
});

test("可以進到子目錄再回上一層", async ({ page }) => {
  await connect(page);
  await page.getByText("etc", { exact: false }).first().click();
  await expect(page.getByText("nginx.conf")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "上一層" }).click();
  await expect(page.getByText("app.log")).toBeVisible({ timeout: 15_000 });
});

test("下載的檔案內容與遠端一致", async ({ page }) => {
  await connect(page);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("tr", { hasText: "readme-中文.txt" }).getByRole("button", { name: "下載" }).click(),
  ]);
  const p = await download.path();
  const got = readFileSync(p!, "utf-8");
  // 位元組要一模一樣 —— 少一個 chunk 或編碼轉錯，畫面上都看不出來
  expect(got).toBe("這是一份含中文檔名與內容的測試檔\n");
});

test("上傳的檔案真的出現在遠端，而且內容正確", async ({ page }) => {
  await connect(page);
  const tmp = join(tmpdir(), "jt-ipam-e2e-upload");
  if (!existsSync(tmp)) mkdirSync(tmp, { recursive: true });
  // 檔名每次不同 —— 用固定檔名的話，上一輪留下的同名檔會讓「表格出現這一列」一開始
  // 就成立，測試於是搶在寫入完成前去讀檔，讀到寫到一半的內容（這條實際上踩過）
  const name = `uploaded-中文-${Date.now()}.conf`;
  const src = `${tmp}/${name}`;
  const payload = "上傳測試\n".repeat(300);
  writeFileSync(src, payload, "utf-8");

  await page.setInputFiles('input[type="file"]', src);
  // 只認表格裡那一列 —— 右上角的提示訊息也含同一個檔名，不能拿它當作「真的傳上去了」
  await expect(page.locator("table").getByText(name)).toBeVisible({ timeout: 30_000 });

  // 直接讀遠端根目錄下的檔案 —— 介面說「已上傳」不算數，檔案在不在才算
  const landed = readFileSync(`${SFTP_ROOT}/${name}`, "utf-8");
  expect(landed).toBe(payload);
  await page.screenshot({ path: "test-results/sftp-upload.png" });
});

test("新增資料夾、改名、刪除都真的作用在遠端", async ({ page }) => {
  await connect(page);
  // 三個操作都會改變遠端主機的檔案系統 —— 每一步都回頭看磁碟，不看畫面提示
  page.on("dialog", () => { /* prompt 由下面的 evaluate 攔截，這裡不處理 */ });

  await page.evaluate(() => { window.prompt = () => "e2e-新資料夾"; });
  await page.getByRole("button", { name: "新增資料夾" }).click();
  await expect(page.locator("table").getByText("e2e-新資料夾")).toBeVisible({ timeout: 15_000 });
  expect(existsSync(`${SFTP_ROOT}/e2e-新資料夾`), "遠端沒有真的建出資料夾").toBe(true);

  await page.evaluate(() => { window.prompt = () => "e2e-改名後"; });
  await page.locator("tr", { hasText: "e2e-新資料夾" })
    .getByRole("button", { name: "重新命名" }).click();
  await expect(page.locator("table").getByText("e2e-改名後")).toBeVisible({ timeout: 15_000 });
  expect(existsSync(`${SFTP_ROOT}/e2e-改名後`), "遠端沒有真的改名").toBe(true);
  expect(existsSync(`${SFTP_ROOT}/e2e-新資料夾`), "舊名字還留著").toBe(false);

  await page.locator("tr", { hasText: "e2e-改名後" })
    .getByRole("button", { name: "刪除" }).click();
  await page.getByRole("button", { name: /確[定認]|是/ }).last().click();
  await expect(page.locator("table").getByText("e2e-改名後")).toBeHidden({ timeout: 15_000 });
  expect(existsSync(`${SFTP_ROOT}/e2e-改名後`), "遠端沒有真的刪掉").toBe(false);
});
