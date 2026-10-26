/**
 * 戰情室 09 IP 物件關係（2026-10-23）：用後端 builder 實際產出的資料（e2e/fixtures/warroom-relations.json，
 * 由 backend/app/services/warroom/relations.py 對測試資料庫產生）模擬 /warroom/pages/relations，檢查：
 * - 1920×1080 不溢出；圖、標籤、節點互不重疊；IP、MAC、CIDR 完整顯示（不加省略號、不被裁掉）
 * - 同一個物件一個節點；來源不一致的邊標「待確認」並列出原因
 * - 沒有權限、未設定虛擬化、沒有焦點時講明狀態，不顯示 0
 * 截圖放 E2E_SHOT_DIR（有設才拍），1920 與 3840 寬、中英文各一輪。
 */
import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

type Payload = Record<string, unknown> & { page: number };
interface Fixture { pages: Payload[]; viewer: Payload; forbidden: Payload; not_configured: Payload; empty: Payload }
const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/warroom-relations.json", import.meta.url), "utf8")) as Fixture;

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const SHOT_DIR = process.env.E2E_SHOT_DIR || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");
test.use({ viewport: { width: 1920, height: 1080 } });

// 只放幾個主題：頁尾的主題列放滿 8 個以上時本來就會擠（另案處理分組導覽），這支只看關係頁本身
const TOPICS = ["overview", "topology", "assets", "relations"];
const RELATIONS_KEY = String(TOPICS.indexOf("relations") + 1);

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

/** 播放清單排滿主題（含 relations）、指定語言（不動伺服器設定，只改這個分頁拿到的設定） */
async function pinConfig(page: Page, language = "") {
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const res = await route.fetch();
    const cfg = await res.json();
    await route.fulfill({ json: { ...cfg, pages: TOPICS, language, reduce_motion: true } });
  });
}

/** 關係頁的資料：page 參數對到第幾份；資料時間改成現在（否則頁首會標成過期） */
async function mockRelations(page: Page, payloads: Payload[]) {
  await page.route(/\/api\/v1\/warroom\/pages\/relations(\?.*)?$/, async (route) => {
    const n = Number(new URL(route.request().url()).searchParams.get("page") || "1");
    const p = payloads[Math.min(Math.max(n, 1), payloads.length) - 1];
    await route.fulfill({ json: { ...p, data_at: new Date().toISOString() } });
  });
}

/** 畫布（1920×1080）裡有沒有東西跑出畫布外 */
async function overflowing(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const w = document.querySelector(".wr") as HTMLElement;
    const r = w.getBoundingClientRect();
    return [...w.querySelectorAll<HTMLElement>("*")]
      .filter((e) => { const b = e.getBoundingClientRect(); return b.width && (b.right > r.right + 1 || b.bottom > r.bottom + 1); })
      .slice(0, 5).map((e) => `${e.tagName}.${e.className}`);
  });
}

/** 版面檢查：標籤與節點互不重疊、IP／MAC／CIDR 沒被裁掉、主區不超出頁尾 */
async function layoutProblems(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const box = (e: Element) => e.getBoundingClientRect();
    const hit = (a: DOMRect, b: DOMRect) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
    const labels = [...document.querySelectorAll("[data-testid=wr-rl-edge] > *")].filter((e) => (e.textContent ?? "").trim());
    const nodes = [...document.querySelectorAll(".rl-node, .rl-ip")];
    labels.forEach((l, i) => {
      labels.slice(i + 1).forEach((m) => { if (hit(box(l), box(m))) out.push(`label/label ${l.textContent}|${m.textContent}`); });
      nodes.forEach((n) => { if (hit(box(l), box(n))) out.push(`label/node ${l.textContent}|${(n.textContent ?? "").slice(0, 30)}`); });
    });
    // 節點裡的文字不可超出節點（兩行名稱用省略號沒關係，但 IP、MAC、CIDR 不可以被切）
    for (const e of document.querySelectorAll<HTMLElement>(".rl-node__mono, .rl-node__s.mono, .rl-ip__v, .rl-ip__m")) {
      const parent = e.closest(".rl-node, .rl-ip")!;
      if (e.scrollWidth > e.clientWidth + 1 || box(e).bottom > box(parent).bottom + 1 || box(e).right > box(parent).right + 1)
        out.push(`clipped ${e.textContent}`);
      if (getComputedStyle(e).textOverflow === "ellipsis") out.push(`ellipsis ${e.textContent}`);
    }
    const main = document.querySelector(".wr-main")!;
    const foot = document.querySelector(".wr-foot")!;
    for (const e of main.querySelectorAll<HTMLElement>(".rl *")) {
      const b = box(e);
      if (b.width && b.bottom > box(foot).top + 1) { out.push(`under footer ${e.className}`); break; }
    }
    return out;
  });
}

async function shot(page: Page, name: string) {
  if (SHOT_DIR) await page.screenshot({ path: `${SHOT_DIR}/${name}.png` });
}

async function openRelations(page: Page) {
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await page.keyboard.press(RELATIONS_KEY);
  await expect(page.locator(".wr-nav__i.on")).toHaveAttribute("data-pos", String(Number(RELATIONS_KEY)));
}

for (const lang of ["zh-TW", "en-US", "ja-JP"]) {
  test(`關係頁逐個焦點：不溢出、不重疊、IP 完整（${lang}）`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await pinConfig(page, lang);
    await mockRelations(page, fixture.pages);
    await login(page);
    await openRelations(page);
    for (let i = 0; i < fixture.pages.length; i++) {
      const p = fixture.pages[i] as unknown as { focus: { ip: { ip: string }; nodes: { key: string }[]; conflicts: string[] } };
      if (i) await page.keyboard.press("PageDown");
      await expect(page.getByTestId("wr-rl-ip")).toContainText(p.focus.ip.ip);
      await page.waitForTimeout(300);
      expect(await overflowing(page), `焦點 ${i + 1}`).toEqual([]);
      expect(await layoutProblems(page), `焦點 ${i + 1}`).toEqual([]);
      // 一個物件一個節點：畫出來的節點數＝資料的節點數（IP 一個）
      expect(await page.locator(".rl-node").count() + 1).toBe(p.focus.nodes.length);
      await expect(page.getByTestId("wr-rl-ip")).toHaveCount(1);
      await expect(page.getByTestId("wr-rl-legend")).toBeVisible();
      if (p.focus.conflicts.length) {
        await expect(page.getByTestId("wr-rl-conflicts")).toBeVisible();
        expect(await page.locator(".rl-el.pending").count()).toBeGreaterThan(0);
      }
      if (lang !== "ja-JP" && i < 3) await shot(page, `relations-${lang}-1920-p${i + 1}`);
    }
    // 頁尾的資料頁碼
    await expect(page.locator(".wr-sub__label")).toBeVisible();
    expect(errors).toEqual([]);
  });
}

for (const lang of ["zh-TW", "en-US"]) {
  test(`4K 等比例放大（${lang}）`, async ({ page }) => {
    await page.setViewportSize({ width: 3840, height: 2160 });
    await pinConfig(page, lang);
    await mockRelations(page, fixture.pages);
    await login(page);
    await openRelations(page);
    await expect(page.getByTestId("wr-rl-graph")).toBeVisible();
    await page.waitForTimeout(300);
    expect(await overflowing(page)).toEqual([]);
    expect(await layoutProblems(page)).toEqual([]);
    await shot(page, `relations-${lang}-3840-p1`);
  });
}

test("沒有權限、未設定虛擬化、沒有焦點：講明狀態，不顯示 0", async ({ page }) => {
  await pinConfig(page, "zh-TW");
  let which: Payload = fixture.viewer;
  await page.route(/\/api\/v1\/warroom\/pages\/relations(\?.*)?$/, async (route) => {
    await route.fulfill({ json: { ...which, data_at: new Date().toISOString() } });
  });
  await login(page);
  await openRelations(page);
  // 只看得到部分範圍：虛擬化列寫「無權限」，指標也寫「無權限」
  await expect(page.getByTestId("wr-rl-none-virtual")).toHaveText("無權限檢視虛擬化資料");
  await expect(page.locator(".wr-metric").nth(2)).toContainText("無權限");
  await expect(page.getByTestId("wr-rl-pending")).toContainText("無權限");
  expect(await layoutProblems(page)).toEqual([]);
  await shot(page, "relations-zh-TW-1920-viewer");

  which = fixture.not_configured;
  await page.keyboard.press("1");
  await page.keyboard.press(RELATIONS_KEY);
  await expect(page.getByTestId("wr-rl-none-virtual")).toHaveText("未設定虛擬化整合");
  await expect(page.locator(".wr-metric").nth(2)).toContainText("未設定");
  await shot(page, "relations-zh-TW-1920-not-configured");

  which = fixture.empty;
  await page.keyboard.press("1");
  await page.keyboard.press(RELATIONS_KEY);
  await expect(page.locator(".rl-center")).toContainText("範圍內沒有與裝置或 VM 有關聯的 IP");
  await shot(page, "relations-zh-TW-1920-empty");

  which = fixture.forbidden;
  await page.keyboard.press("1");
  await page.keyboard.press(RELATIONS_KEY);
  await expect(page.locator(".rl-center")).toContainText("無權限檢視");
  await expect(page.getByTestId("wr-rl-counts")).toContainText("無權限");
  expect(await page.locator(".wr-metric__v").count()).toBe(0);         // 三個指標都不是數字
  await shot(page, "relations-zh-TW-1920-forbidden");
});
