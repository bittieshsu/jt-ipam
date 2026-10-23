/**
 * 戰情室 08 IP 統計、10 IP 可用性追蹤（2026-10-23）：版面不溢出、不重疊、IP 不被截斷、圖例有文字、
 * 各種狀態（無權限、沒有追蹤、歷史不足降級、24 小時／90 日）都講得清楚；1920 與 3840 寬各截一張圖。
 * 頁面資料用 page.route 換成後端 builder 實際產生的回應（helpers/warroomStatsAvailMocks.ts）：e2e 用的後端是共用的，
 * 不一定是這一版。資料口徑與權限在 backend/tests/test_warroom_stats.py、test_warroom_availability.py。
 */
import { test, expect, type Page } from "@playwright/test";
import {
  AV_24H, AV_90D, AV_FALLBACK, AV_P1, AV_P2, STATS_P1, STATS_P2, STATS_P3,
} from "./helpers/warroomStatsAvailMocks";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const SHOTS = process.env.E2E_SHOT_DIR || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");
test.use({ viewport: { width: 1920, height: 1080 } });

const TOPICS = ["overview", "capacity", "stats", "availability"];
type Payload = Record<string, unknown>;

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

/** 播放清單含這兩個主題、指定語言（不動伺服器設定，只改這個分頁拿到的設定） */
async function pinConfig(page: Page, language = "") {
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const res = await route.fetch();
    const cfg = await res.json();
    await route.fulfill({ json: { ...cfg, pages: TOPICS, language, reduce_motion: true } });
  });
}

/** 這兩個主題的頁面資料：依頁碼給對應的回應，資料時間改成現在（否則頁首會標資料逾時） */
async function mockPages(page: Page, stats: Payload[], av: Payload[]) {
  await page.route(/\/api\/v1\/warroom\/pages\/(stats|availability)(\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const key = url.pathname.endsWith("stats") ? "stats" : "availability";
    const list = key === "stats" ? stats : av;
    const n = Math.min(Math.max(Number(url.searchParams.get("page") || 1), 1), list.length);
    await route.fulfill({ json: { ...list[n - 1], data_at: new Date().toISOString() } });
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

/** 被截斷的文字（scrollWidth 比 clientWidth 大，或用了省略號）：IP、CIDR 不可以被截斷 */
async function truncated(page: Page, selector: string): Promise<string[]> {
  return page.evaluate((sel) => [...document.querySelectorAll<HTMLElement>(sel)]
    .filter((e) => e.scrollWidth > e.clientWidth + 1 || getComputedStyle(e).textOverflow === "ellipsis")
    .map((e) => e.textContent ?? ""), selector);
}

/** a 的下緣不可以超過 b 的上緣（頁尾、內容、分頁各有固定區域，不可互相覆蓋） */
async function above(page: Page, a: string, b: string): Promise<boolean> {
  return page.evaluate(([sa, sb]) => {
    const as = [...document.querySelectorAll(sa)];
    const eb = document.querySelector(sb);
    if (!as.length || !eb) return true;
    const top = eb.getBoundingClientRect().top;
    return as.every((e) => e.getBoundingClientRect().bottom <= top + 1);
  }, [a, b] as const);
}

async function open(page: Page, n: number) {
  await page.keyboard.press(String(n));
  await expect(page.locator(".wr-nav__i.on")).toContainText(String(n).padStart(2, "0"));
  await page.waitForTimeout(400);
}

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` });
}

for (const lang of ["zh-TW", "en-US", "ja-JP"]) {
  test(`統計與可用性：不溢出、不重疊、IP 不截斷（${lang}）`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await pinConfig(page, lang);
    await mockPages(page, [STATS_P1, STATS_P2, STATS_P3], [AV_P1, AV_P2]);
    await login(page);
    await page.goto("/warroom");
    await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });

    // ── 08 IP 統計 ──
    await open(page, 3);
    await expect(page.getByTestId("wr-st-row")).toHaveCount(3);
    expect(await overflowing(page)).toEqual([]);
    // 三列相加＝已登記 IP，畫面寫出來的就是這個總數
    const counts = await page.getByTestId("wr-st-count").allInnerTexts();
    const sum = counts.reduce((a, s) => a + Number(s.replace(/[^\d]/g, "")), 0);
    expect(sum).toBe((STATS_P1.status as { total: number }).total);
    await expect(page.getByTestId("wr-st-sum")).toContainText(sum.toLocaleString("en-US"));
    // 共用尺度寫在畫面上
    await expect(page.getByTestId("wr-st-scale")).toContainText(String((STATS_P1.status as { unit: number }).unit));
    // 排行每頁不超過六個，IP／CIDR 不截斷
    const items = await page.getByTestId("wr-st-item").count();
    expect(items).toBeGreaterThanOrEqual(4);
    expect(items).toBeLessThanOrEqual(6);
    expect(await truncated(page, ".st-item__c")).toEqual([]);
    expect(await above(page, ".st-row, .st-sum", ".st-foot")).toBe(true);
    expect(await above(page, ".st-item", ".st-foot")).toBe(true);
    // 指標四個；稽核事件用中性色（不是告警的珊瑚橘）
    await expect(page.locator(".wr-metric")).toHaveCount(4);
    await expect(page.locator(".wr-metric").nth(3)).toHaveClass(/t-ink/);
    const labelLines = await page.evaluate(() => [...document.querySelectorAll(".wr-metric__l")].map((e) => {
      const r = document.createRange(); r.selectNodeContents(e);
      return new Set([...r.getClientRects()].map((b) => Math.round(b.top))).size;
    }));
    for (const n of labelLines) expect(n).toBeLessThanOrEqual(2);
    await shot(page, `stats-1920-${lang}`);
    // 換資料頁只換排行：分布的數字不變
    await page.keyboard.press("PageDown");
    await page.waitForTimeout(600);
    await expect(page.getByTestId("wr-st-item").first()).toContainText("05");
    expect(await page.getByTestId("wr-st-count").allInnerTexts()).toEqual(counts);
    expect(await overflowing(page)).toEqual([]);

    // ── 10 IP 可用性追蹤 ──
    await open(page, 4);
    const rows = page.getByTestId("wr-av-row");
    await expect(rows).toHaveCount((AV_P1.rows as unknown[]).length);
    expect(await rows.count()).toBeLessThanOrEqual(8);
    expect(await overflowing(page)).toEqual([]);
    const cellsPerRow = await page.evaluate(() => [...document.querySelectorAll("[data-testid=wr-av-row]")]
      .map((r) => r.querySelectorAll(".av-c").length));
    expect(new Set(cellsPerRow)).toEqual(new Set([(AV_P1.window as { cells: number }).cells]));
    // 圖例都有文字；只有已定義的狀態類別
    const legend = await page.locator(".av-legend__i").allInnerTexts();
    expect(legend).toHaveLength(4);
    for (const l of legend) expect(l.trim().length).toBeGreaterThan(0);
    const classes = await page.evaluate(() => [...new Set([...document.querySelectorAll(".av-bars .av-c")]
      .map((c) => [...c.classList].find((x) => x.startsWith("c-"))))]);
    for (const c of classes) expect(["c-online", "c-offline", "c-mixed", "c-unknown", "c-future"]).toContain(c);
    // IP 不截斷；列表不蓋到圖例；事件欄不超出
    expect(await truncated(page, ".av-row__ip, .av-ev__ip, .av-ev__solo")).toEqual([]);
    expect(await above(page, "[data-testid=wr-av-row]", ".av-foot")).toBe(true);
    expect(await above(page, ".av-foot", ".wr-foot")).toBe(true);
    await expect(page.getByTestId("wr-av-event").first()).toBeVisible();
    // 不是應用服務 SLA 的說明一直在畫面上
    await expect(page.locator(".av-notes")).toContainText(/SLA/);
    await expect(page.locator(".wr-metric")).toHaveCount(3);
    await shot(page, `availability-1920-${lang}`);
    await page.keyboard.press("PageDown");
    await page.waitForTimeout(600);
    expect(await overflowing(page)).toEqual([]);
    expect(errors).toEqual([]);
  });
}

test("可用性：24 小時、90 日、歷史不足降級、沒有追蹤清單", async ({ page }) => {
  await pinConfig(page, "zh-TW");
  let av: Payload[] = [AV_24H];
  await page.route(/\/api\/v1\/warroom\/pages\/availability(\?.*)?$/, async (route) => {
    await route.fulfill({ json: { ...av[0], data_at: new Date().toISOString() } });
  });
  await page.route(/\/api\/v1\/warroom\/pages\/stats(\?.*)?$/, (r) => r.fulfill({ json: { ...STATS_P1, data_at: new Date().toISOString() } }));
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });

  await open(page, 4);
  await expect(page.locator(".av-h")).toContainText("最近 24 小時");
  await expect(page.locator(".av-h")).toContainText("每格 1 小時");
  expect(await overflowing(page)).toEqual([]);
  await shot(page, "availability-24h-1920-zh-TW");

  av = [AV_90D];
  await open(page, 3);
  await open(page, 4);
  await expect(page.locator(".av-h")).toContainText("最近 90 日");
  expect(await overflowing(page)).toEqual([]);
  const widest = await page.evaluate(() => Math.max(...[...document.querySelectorAll(".av-bars")].map((b) => b.getBoundingClientRect().right)));
  const statusLeft = await page.evaluate(() => Math.min(...[...document.querySelectorAll(".av-row__st")].map((b) => b.getBoundingClientRect().left)));
  expect(widest).toBeLessThanOrEqual(statusLeft);              // 90 格也不會壓到右邊的狀態欄
  await shot(page, "availability-90d-1920-zh-TW");

  av = [AV_FALLBACK];
  await open(page, 3);
  await open(page, 4);
  await expect(page.getByTestId("wr-av-fallback")).toContainText("歷史資料不足 30 日，改顯示 7 日");

  av = [{ ...AV_P1, rows: [], events: [], total: 0, pages: 1, metrics: { tracked: 0, down_now: 0, flapping: 0 },
          body: { state: "empty", excluded: 0 }, last_valid_at: null }];
  await open(page, 3);
  await open(page, 4);
  await expect(page.getByTestId("wr-av-empty")).toBeVisible();
  await expect(page.getByTestId("wr-av-empty")).toContainText("可用性追蹤");
  expect(await overflowing(page)).toEqual([]);
});

test("統計：大數字、無權限看稽核、沒有資料", async ({ page }) => {
  await pinConfig(page, "en-US");
  const big = {
    ...STATS_P1,
    metrics: { registered_ips: 1234567, subnets: 23456, sections: 345, audit_24h: null },
    status: { ...(STATS_P1.status as object), total: 1234567, unit: 5000, rows: [
      { status: "online", count: 765432, cells: 154, pct: 62.0 },
      { status: "offline", count: 123456, cells: 25, pct: 10.0 },
      { status: "unknown", count: 345679, cells: 70, pct: 28.0 },
    ] },
    families: { v4: 1200000, v6: 34567 },
  };
  let stats: Payload = big;
  await page.route(/\/api\/v1\/warroom\/pages\/stats(\?.*)?$/, (r) => r.fulfill({ json: { ...stats, data_at: new Date().toISOString() } }));
  await page.route(/\/api\/v1\/warroom\/pages\/availability(\?.*)?$/, (r) => r.fulfill({ json: { ...AV_P1, data_at: new Date().toISOString() } }));
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await open(page, 3);
  expect(await overflowing(page)).toEqual([]);
  await expect(page.locator(".wr-metric").nth(3)).toContainText(/No access|無權限/);   // null 不顯示成 0
  await expect(page.locator(".wr-metric").nth(0)).toContainText("1,234,567");
  await expect(page.getByTestId("wr-st-notes")).not.toContainText(/Audit events are/);
  await shot(page, "stats-big-1920-en-US");

  stats = { ...STATS_P1, metrics: { registered_ips: 0, subnets: 0, sections: 0, audit_24h: 0 }, total: 0, pages: 1,
            status: { ...(STATS_P1.status as object), state: "empty", total: 0, unit: 1,
                      rows: ["online", "offline", "unknown"].map((s) => ({ status: s, count: 0, cells: 0, pct: null })) },
            ranking: { state: "empty", total: 0, items: [] }, families: { v4: 0, v6: 0 } };
  await open(page, 4);
  await open(page, 3);
  await expect(page.locator(".st-dist .wr-empty")).toBeVisible();
  expect(await overflowing(page)).toEqual([]);
});

test.describe("4K", () => {
  test.use({ viewport: { width: 3840, height: 2160 } });
  for (const lang of ["zh-TW", "en-US"]) {
    test(`3840 寬等比放大（${lang}）`, async ({ page }) => {
      await pinConfig(page, lang);
      await mockPages(page, [STATS_P1, STATS_P2, STATS_P3], [AV_P1, AV_P2]);
      await login(page);
      await page.goto("/warroom");
      await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
      await open(page, 3);
      expect(await overflowing(page)).toEqual([]);
      await shot(page, `stats-3840-${lang}`);
      await open(page, 4);
      expect(await overflowing(page)).toEqual([]);
      await shot(page, `availability-3840-${lang}`);
    });
  }
});
