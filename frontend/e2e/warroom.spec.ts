/**
 * 戰情室（2026-10-21 起）：七個主題逐頁打開、版面不溢出、按鍵操作、暫停閃光、中英文各一輪、設定頁。
 * 使用者 10-22：「戰情室的所有功能、頁面、顯示也要加入測試計劃」—— 手動項目在 TEST_CHECKLIST 的戰情室章節，
 * 這支守住看得出來的那部分（資料正確性與權限在 backend/tests/test_warroom*.py）。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");
test.use({ viewport: { width: 1920, height: 1080 } });

const TOPICS = ["overview", "capacity", "topology", "services", "anomalies", "assets", "collection"];

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

/** 播放清單排滿七個主題、指定語言（不動伺服器設定，只改這個分頁拿到的設定） */
async function pinConfig(page: Page, language = "") {
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const res = await route.fetch();
    const cfg = await res.json();
    await route.fulfill({ json: { ...cfg, pages: TOPICS, language, reduce_motion: true } });
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

for (const lang of ["zh-TW", "en-US", "ja-JP"]) {
  test(`七個主題逐頁打開、不溢出（${lang}）`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await pinConfig(page, lang);
    await login(page);
    await page.goto("/warroom");
    await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
    const titles = new Set<string>();
    for (let i = 1; i <= TOPICS.length; i++) {
      await page.keyboard.press(String(i));
      await expect(page.locator(".wr-nav__i.on")).toContainText(String(i).padStart(2, "0"));
      await page.waitForTimeout(400);
      titles.add(await page.locator(".wr-title h1").innerText());
      expect(await overflowing(page), `主題 ${i}`).toEqual([]);
      // 指標名稱最多兩行、主題名稱最多兩行（英文曾經三行，使用者 10-22：「必要時縮寫」）
      const lines = await page.evaluate(() => {
        const count = (e: Element) => { const r = document.createRange(); r.selectNodeContents(e);
          return new Set([...r.getClientRects()].map((b) => Math.round(b.top))).size; };
        return { labels: [...document.querySelectorAll(".wr-metric__l")].map((e) => [e.textContent, count(e)] as const),
                 title: count(document.querySelector(".wr-title h1")!) };
      });
      for (const [text, n] of lines.labels) expect(n, `主題 ${i}「${text}」`).toBeLessThanOrEqual(2);
      expect(lines.title, `主題 ${i} 標題`).toBeLessThanOrEqual(2);
      // 採集頁的整合來源最多兩排，而且不可以蓋到上面的代理卡片（10-22 換行後曾經疊在卡片上）
      const overlap = await page.evaluate(() => {
        const foot = document.querySelector("[data-testid=wr-sources]");
        if (!foot) return null;
        const top = foot.getBoundingClientRect().top;
        const rows = new Set([...foot.querySelectorAll<HTMLElement>("[data-src]")].map((c) => c.offsetTop)).size;
        const cards = [...document.querySelectorAll(".col-q")].filter((c) => c.getBoundingClientRect().bottom > top + 1).length;
        return { rows, cards };
      });
      if (overlap) {
        expect(overlap.cards, `主題 ${i} 整合來源蓋到卡片`).toBe(0);
        expect(overlap.rows, `主題 ${i} 整合來源排數`).toBeLessThanOrEqual(2);
      }
      // 每個主題都要畫出內容或講明狀態（沒有資料／無權限），不可以整塊空白
      await expect(page.locator(".wr-main")).not.toBeEmpty();
    }
    expect(titles.size).toBe(TOPICS.length);
    expect(errors).toEqual([]);
  });
}

test("按鍵：← → 換主題、↑ ↓／PgUp PgDn 在同主題內換資料頁、空白鍵暫停會閃、H 開按鍵說明", async ({ page }) => {
  await pinConfig(page);
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  const nav = page.locator(".wr-nav__i.on");
  await expect(nav).toContainText("01");
  await page.keyboard.press("ArrowRight");
  await expect(nav).toContainText("02");
  await page.keyboard.press("ArrowLeft");
  await expect(nav).toContainText("01");
  await page.keyboard.press("ArrowLeft");                       // 從第一個往前繞到最後一個
  await expect(nav).toContainText("07");
  // 資料頁只在同一個主題內循環：只有一頁時按往下留在原主題（10-22 起不再跳到下一個主題）
  await page.keyboard.press("1");
  await expect(nav).toContainText("01");
  await page.keyboard.press("PageDown");
  await page.waitForTimeout(400);
  await expect(nav).toContainText("01");
  // 暫停：頁首標示、進度線消失、畫面閃一下；再按一次恢復也會閃
  await page.keyboard.press(" ");
  await expect(page.getByTestId("wr-flash")).toBeAttached();
  await expect(page.locator(".wr-paused")).toBeVisible();
  await expect(page.getByTestId("wr-progress")).toHaveCount(0);
  await page.waitForTimeout(600);
  await page.keyboard.press(" ");
  await expect(page.getByTestId("wr-flash")).toBeAttached();
  await expect(page.locator(".wr-paused")).toHaveCount(0);
  await expect(page.getByTestId("wr-progress")).toHaveCount(1);
  // 按鍵說明
  await page.keyboard.press("?");                               // 問號不再有作用（只用 H）
  await page.waitForTimeout(200);
  await expect(page.getByTestId("wr-help")).toHaveCount(0);
  await page.keyboard.press("h");
  await expect(page.getByTestId("wr-help")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("wr-help")).toHaveCount(0);
  await page.keyboard.press("h");
  await expect(page.getByTestId("wr-help")).toBeVisible();
  await page.keyboard.press("h");
  await expect(page.getByTestId("wr-help")).toHaveCount(0);
  // 按鍵提示常駐（10-22 起不必先動滑鼠）
  await expect(page.locator(".wr-keyhint")).toContainText("H");
});

test("戰情室設定：顯示範圍三個選單、按鍵表、顯示權杖沒選帳號不能存", async ({ page }) => {
  await login(page);
  await page.goto("/warroom-settings");
  for (const k of ["customers", "sections", "subnets"]) await expect(page.getByTestId(`wrs-scope-${k}`)).toBeVisible();
  await expect(page.locator(".wrs-keys kbd").first()).toBeVisible();
  await expect(page.getByTestId("wrs-display-saved")).toBeVisible();
});

test("戰情室設定：可選可用性主題的時間範圍，存檔會送出（10-23）", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  // 不真的改伺服器設定（別的測試同時在讀）：攔下 PUT，回原本的設定加上送出的值
  let cur: Record<string, unknown> = {};
  await page.route(/\/api\/v1\/system\/warroom$/, async (route) => {
    if (route.request().method() === "GET") {
      cur = await (await route.fetch()).json();
      return route.fulfill({ json: cur });
    }
    if (route.request().method() !== "PUT") return route.fallback();
    sent = route.request().postDataJSON();
    await route.fulfill({ json: { ...cur, availability_window: sent?.availability_window } });
  });
  await login(page);
  await page.goto("/warroom-settings");
  const sel = page.getByTestId("wrs-availability-window");
  await expect(sel).toContainText("最近 7 天");
  await sel.click();
  await page.locator(".n-base-select-option", { hasText: "最近 30 天" }).click();
  await page.getByRole("button", { name: /儲存/ }).last().click();
  await expect.poll(() => sent?.availability_window).toBe("30d");
  await expect(sel).toContainText("最近 30 天");
});

/** 正式站台的數量級（10-22 使用者截圖：資產頁「47 / 72」被裁掉、異常頁三位數旁的標籤一字一行）。
 *  本機資料太少看不出來，這裡把 API 回應換成正式站台的量。 */
test("正式站台的數量級：資產、異常不溢出，觀測開始前就有的不算新出現", async ({ page }) => {
  await pinConfig(page);
  const cells = (n: number, a: string, b: string, hit: number) => Array.from({ length: n }, (_, i) => (i < hit ? a : b));
  await page.route(/\/api\/v1\/warroom\/pages\/assets/, async (route) => {
    const d = await (await route.fetch()).json();
    d.metrics = { hosts: 6, vms: 151, monitor_gaps: 31 };
    d.coverage = { state: "ok", with_ip: 72, no_ip: 5, systems: [
      { kind: "wazuh", covered: 47, gap: 25, total: 72, unit: 2, cells: cells(36, "covered", "gap", 24) },
      { kind: "ocs", covered: 51, gap: 21, total: 72, unit: 2, cells: cells(36, "covered", "gap", 26) },
      { kind: "zabbix", covered: 1234, gap: 266, total: 1500, unit: 38, cells: cells(40, "covered", "gap", 33) }] };
    await route.fulfill({ json: d });
  });
  await page.route(/\/api\/v1\/warroom\/pages\/anomalies/, async (route) => {
    const d = await (await route.fetch()).json();
    const start = Date.now() - 6 * 3600_000;
    const row = (category: string, open: number) => ({ category, open, seen: open,
      cells: [...Array(6).fill("nodata"), ...Array(12).fill("active")] });
    d.metrics = { open_anomalies: 149, new_6h: 0, resolved_6h: 0 };
    d.timeline = { state: "ok", slots: 18, slot_minutes: 20, start: new Date(start).toISOString(),
      data_since: new Date(start + 2 * 3600_000).toISOString(), more: 6,
      rows: [row("offline", 86), row("kind_os_changed", 27), row("unauthorized", 10), row("dangling_dns", 9), row("exposed", 6)] };
    d.priority = { state: "ok", total: 149, page: 1, pages: 50, items: [1, 2, 3].map((n) => ({
      id: String(n), rank: n, category: "unauthorized", label: `192.0.2.${n}`, ip: `192.0.2.${n}`,
      severity: "high", first_seen_at: new Date(start + 2 * 3600_000).toISOString(), baseline: true })) };
    await route.fulfill({ json: d });
  });
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  for (const key of ["6", "5"]) {
    await page.keyboard.press(key);
    await page.waitForTimeout(500);
    expect(await overflowing(page), `主題 ${key}`).toEqual([]);
    // 每一塊都要整塊在自己的區塊裡（右邊數字不可以被裁掉）
    const clipped = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".as-sys__v, .wr-metric__l")]
      .filter((e) => { const box = e.closest("section, .wr-metric") as HTMLElement; const a = e.getBoundingClientRect(), b = box.getBoundingClientRect();
        return a.right > b.right + 1; }).map((e) => e.innerText));
    expect(clipped, `主題 ${key} 被裁掉的文字`).toEqual([]);
    // 指標名稱不可以只剩一個字在第二行
    const orphan = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".wr-metric__l")].filter((e) => {
      const r = document.createRange(); r.selectNodeContents(e); const lines = [...r.getClientRects()];
      if (lines.length < 2) return false;
      const last = lines[lines.length - 1]; return last.width < 40;
    }).map((e) => e.innerText));
    expect(orphan, `主題 ${key} 一字一行`).toEqual([]);
  }
  await expect(page.locator(".an-item__v").filter({ hasText: "觀測開始前" }).first()).toBeVisible();
});

test("同主題換資料頁只換分頁的那一區，標題與其他區塊不重畫；右下箭頭是上下、按鍵提示常駐", async ({ page }) => {
  await pinConfig(page);
  await page.route(/\/api\/v1\/warroom\/pages\/anomalies/, async (route) => {
    const d = await (await route.fetch()).json();
    const p = Number(new URL(route.request().url()).searchParams.get("page") || 1);
    d.page = p; d.pages = 3; d.total = 9;
    d.priority = { state: "ok", total: 9, page: p, pages: 3, items: [1, 2, 3].map((n) => ({
      id: `${p}-${n}`, rank: (p - 1) * 3 + n, category: "unauthorized", label: `192.0.2.${(p - 1) * 3 + n}`,
      ip: `192.0.2.${(p - 1) * 3 + n}`, severity: "high", first_seen_at: new Date().toISOString(), baseline: false })) };
    await route.fulfill({ json: d });
  });
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("wr-keyhint")).toBeVisible();          // 沒動滑鼠也看得到
  await page.keyboard.press("5");
  await expect(page.locator(".an-item").first()).toContainText("192.0.2.1");
  await page.evaluate(() => {
    (document.querySelector(".wr-title") as HTMLElement).dataset.mark = "same";
    (document.querySelector(".an-tl") as HTMLElement).dataset.mark = "same";
  });
  await expect(page.getByTestId("wr-page-next").locator("svg")).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await expect(page.locator(".an-item").first()).toContainText("192.0.2.4");
  expect(await page.locator(".wr-title[data-mark=same]").count()).toBe(1);
  expect(await page.locator(".an-tl[data-mark=same]").count()).toBe(1);
  await page.getByTestId("wr-page-prev").click();
  await expect(page.locator(".an-item").first()).toContainText("192.0.2.1");
  // 第 1 頁再往上：到同主題的最後一頁（第 3 頁），不換主題；最後一頁往下回第 1 頁
  await page.keyboard.press("PageUp");
  await expect(page.locator(".an-item").first()).toContainText("192.0.2.7");
  await expect(page.locator(".wr-nav__i.on")).toContainText("05");
  await page.keyboard.press("ArrowDown");
  await expect(page.locator(".an-item").first()).toContainText("192.0.2.1");
  await expect(page.locator(".wr-nav__i.on")).toContainText("05");
  expect(await page.locator(".wr-title[data-mark=same]").count()).toBe(1);
  await page.keyboard.press("ArrowRight");                                   // 換主題才整頁換
  await expect(page.locator(".wr-nav__i.on")).toContainText("06");
  await expect(page.locator(".wr-title[data-mark=same]")).toHaveCount(0);
});

test("按鍵說明：電視與設定頁列的是同一份（含 H、Home、Esc）", async ({ page }) => {
  await pinConfig(page);
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await page.keyboard.press("h");
  const tv = await page.getByTestId("wr-help").locator("kbd").allInnerTexts();
  await page.goto("/warroom-settings");
  await expect(page.locator(".wrs-keys kbd").first()).toBeVisible();
  const settings = await page.locator(".wrs-keys kbd").allInnerTexts();
  expect(settings).toEqual(tv);
  for (const k of ["H", "Home", "Esc"]) expect(tv).toContain(k);
  expect(tv).not.toContain("?");
});

test("暫停只是不換頁，資料照樣定時更新（同一頁、不換主題）", async ({ page }) => {
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const cfg = await (await route.fetch()).json();
    await route.fulfill({ json: { ...cfg, pages: TOPICS, dwell_seconds: 20, reduce_motion: true } });
  });
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await page.clock.install();
  let fetched = 0;
  page.on("request", (r) => { if (/\/api\/v1\/warroom\/pages\//.test(r.url())) fetched++; });
  await page.keyboard.press(" ");
  await expect(page.locator(".wr-paused")).toBeVisible();
  const before = await page.locator(".wr-nav__i.on").innerText();
  await page.clock.fastForward(31_000);
  await expect.poll(() => fetched).toBeGreaterThanOrEqual(1);
  expect(await page.locator(".wr-nav__i.on").innerText()).toBe(before);   // 沒有換主題
  await expect(page.locator(".wr-paused")).toBeVisible();
});

test("播放清單可以拖拉排序；電視的編號與數字鍵照播放順序", async ({ page }) => {
  await login(page);
  await page.goto("/warroom-settings");
  const items = page.getByTestId("wrs-page");
  await expect(items.first()).toBeVisible();
  // 拖最後一個「有在播」的主題（08 之後的新主題預設不加入播放清單，編號是「–」）
  const nums = (await items.locator(".rank-num").allInnerTexts()).map((x) => x.trim());
  const last = nums.findLastIndex((x) => x !== "–");
  const lastLabel = (await items.nth(last).locator(".rank-label").innerText()).trim();
  await items.nth(last).dragTo(items.first());
  await expect(items.first().locator(".rank-label")).toHaveText(lastLabel);
  await expect(items.first().locator(".rank-num")).toHaveText("01");
  // 關掉一個：它變成「–」，後面的編號往前補
  await items.nth(1).locator(".n-switch").click();
  await expect(items.nth(1).locator(".rank-num")).toHaveText("–");
  await expect(items.nth(2).locator(".rank-num")).toHaveText("02");

  // 電視：照播放順序編號（不存設定，只改這個分頁拿到的設定）
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const cfg = await (await route.fetch()).json();
    await route.fulfill({ json: { ...cfg, pages: ["collection", "overview", "anomalies"], language: "zh-TW", reduce_motion: true } });
  });
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  const nav = page.locator(".wr-nav__i");
  await expect(nav.nth(0)).toContainText("01");
  await expect(nav.nth(0)).toContainText("採集");
  await page.keyboard.press("3");
  await expect(page.locator(".wr-nav__i.on")).toContainText("異常");
  await expect(page.locator(".wr-nav__i.on")).toContainText("03");
});

/** 區塊內容有沒有超出自己的框（區塊 overflow:hidden 時 scrollHeight 會大於 clientHeight） */
async function clipped(page: Page, sel: string): Promise<number[][]> {
  return page.evaluate((s) => [...document.querySelectorAll<HTMLElement>(s)]
    .map((e) => [e.scrollHeight, e.clientHeight]).filter(([a, b]) => a > b + 1), sel);
}

test("滿版資料不截斷：三個地點、DNS 來源滿格、判斷線索逐項排開、條帶有說明（10-22）", async ({ page }) => {
  await pinConfig(page);
  const ip = (n: number) => `192.0.2.${n}`;
  await page.route(/\/api\/v1\/warroom\/pages\/overview/, async (route) => {
    const d = await (await route.fetch()).json();
    const g = (id: string, name: string | null, n: number) => ({ id, name, subnets: n, unit: 1,
      counts: { ok: 0, warn: 0, anomaly: 2, stale: 0, unscanned: n - 2 }, cells: Array.from({ length: n }, (_, i) => (i < 2 ? "anomaly" : "unscanned")) });
    d.group_by = "location";
    d.groups = [g("a", "公司主機房", 3), g("b", "公司異地備援機房", 3), g(null as unknown as string, null, 21)];
    d.priority = { state: "ok", total: 149, items: [1, 2, 3, 4].map((n) => ({ kind: "anomaly", category: "unauthorized_ip",
      value: ip(n), severity: "high", at: new Date().toISOString(),
      hints: { vm: "app-vm-01", vendor: "Proxmox Server Solutions GmbH", mac: "bc:24:11:00:00:31" } })) };
    await route.fulfill({ json: d });
  });
  await page.route(/\/api\/v1\/warroom\/pages\/services/, async (route) => {
    const d = await (await route.fetch()).json();
    if (d.dns?.group?.sources) for (const s of d.dns.group.sources) {
      s.cells = Array.from({ length: 72 }, (_, i) => (i < 60 ? "ok" : i < 66 ? "partial" : "missing"));
      s.unit = 2; s.counts = { ok: 120, partial: 12, pending: 0, missing: 12 };
    }
    await route.fulfill({ json: d });
  });
  await page.route(/\/api\/v1\/warroom\/pages\/anomalies/, async (route) => {
    const d = await (await route.fetch()).json();
    const cells = [...Array(18).fill("active")];
    d.timeline = { ...(d.timeline ?? {}), state: "ok", slots: 18, more: 6,
      rows: ["stale_ips", "kind_change", "unauthorized_ips", "stale_dns", "external_exposure"].map((c, i) => ({ category: c, open: 80 - i * 10, seen: 1, cells })) };
    d.priority = { state: "ok", total: 149, items: [1, 2, 3].map((n) => ({ id: `x${n}`, rank: n, category: "unauthorized_ips",
      label: ip(n), ip: ip(n), severity: "high", first_seen_at: new Date().toISOString(), baseline: true,
      hints: { vm: "app-vm-01", device_kind: "server", os: "Ubuntu 24.04", vendor: "Proxmox Server Solutions GmbH",
               mac: "bc:24:11:00:00:31" } })) };
    await route.fulfill({ json: d });
  });
  await login(page);
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible();

  // 05 異常：右側卡片很高時，左下「對外開放服務的證據」仍整個在畫面內（10-22 使用者截圖）
  await page.keyboard.press("5");
  const evBox = await page.getByTestId("wr-an-evidence").boundingBox();
  const foot = await page.locator(".wr-foot").boundingBox();
  expect(evBox!.y + evBox!.height).toBeLessThanOrEqual(foot!.y + 1);
  // 右側每張待處理異常卡片的內容都在卡片內，最後一張也在頁尾之上（10-23「超出格子」）
  expect(await clipped(page, ".an-item")).toEqual([]);
  const lastCard = await page.getByTestId("wr-an-item").last().boundingBox();
  expect(lastCard!.y + lastCard!.height).toBeLessThanOrEqual(foot!.y + 1);

  await page.keyboard.press("1");
  await expect(page.getByTestId("wr-group")).toHaveCount(3);
  expect(await clipped(page, ".ov-group")).toEqual([]);
  // 第三個地點的最後一行（未掃描）要在畫面內
  const last = page.getByTestId("wr-group").nth(2).locator(".ov-count").last();
  const wr = await page.locator(".wr").boundingBox();
  const lb = await last.boundingBox();
  expect(lb!.y + lb!.height).toBeLessThanOrEqual(wr!.y + wr!.height);
  // 線索是逐項的圖示＋文字，不是一串「·」
  const hint = page.getByTestId("wr-hints").first();
  await expect(hint.locator(".wr-hint")).toHaveCount(3);
  await expect(hint).not.toContainText("·");

  await page.keyboard.press("4");
  await expect(page.getByTestId("wr-dns-source").first()).toBeVisible();
  expect(await clipped(page, ".svc-q")).toEqual([]);

  await page.keyboard.press("7");
  const agents = page.getByTestId("wr-agent");
  if (await agents.count()) await expect(page.getByTestId("wr-strip-caption").first()).toContainText(/最近 \d+ 輪/);
  await expect(page.getByTestId("wr-sources")).not.toContainText("最近備份");
});

test("資料時間落後太久：右上變紅並出現警示圖示（10-23）", async ({ page }) => {
  await pinConfig(page);
  await page.route(/\/api\/v1\/warroom\/pages\//, async (route) => {
    const d = await (await route.fetch()).json();
    d.data_at = new Date(Date.now() - 60 * 60_000).toISOString();     // 一小時前的資料
    await route.fulfill({ json: d });
  });
  await login(page);
  await page.goto("/warroom");
  const at = page.getByTestId("wr-data-at");
  await expect(at).toHaveAttribute("data-stale", "1");
  await expect(at.locator(".wr-ico")).toBeVisible();
  await expect(at).toHaveClass(/wr-off/);
});

test("偵測到新版：網站準備好後自動重新載入，接著播原本的主題（10-23）", async ({ page }) => {
  await pinConfig(page);
  let build = "e2e-A";
  await page.route(/\/version\.json/, (route) => route.fulfill({ json: { version: "0.0.0-e2e", build } }));
  await login(page);
  await page.clock.install();                                       // 要在開頁面之前裝：載入時就設好的計時器才快轉得到
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible();
  await page.keyboard.press("4");                                   // 停在 04 服務
  await expect(page.locator(".wr-nav__i.on")).toContainText("04");
  build = "e2e-B";                                                  // 部署了新版
  const reloaded = page.waitForEvent("load");
  await page.clock.fastForward(181_000);
  await reloaded;
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".wr-nav__i.on")).toContainText("04");  // 重新載入後接著播 04
});

test("播放清單排滿 11 個主題：頁尾放不下就只留編號、正在播的寫名稱，不蓋到按鍵提示（10-23）", async ({ page }) => {
  const ALL = [...TOPICS, "stats", "relations", "availability", "ai_audit"];
  let language = "en-US";
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const cfg = await (await route.fetch()).json();
    await route.fulfill({ json: { ...cfg, pages: ALL, language, reduce_motion: true } });
  });
  await login(page);
  for (language of ["en-US", "zh-TW", "ja-JP"]) {
    await page.goto("/warroom");
    const nav = page.getByTestId("wr-nav");
    await expect(nav.locator(".wr-nav__i")).toHaveCount(11);
    await expect(nav.locator(".wr-nav__i.on")).toContainText(/\S{2,}/);
    // 換到有分頁的主題：右邊多出「子網路 01–04 / 8」這類文字，主題列可用寬度變小後要重新判斷（中文曾被切成「11 AI 巡」）
    await page.keyboard.press("8");
    await expect(nav.locator(".wr-nav__i.on")).toContainText("08");
    await page.waitForTimeout(600);
    const box = await page.evaluate(() => {
      const n = document.querySelector("[data-testid=wr-nav]") as HTMLElement;
      const s = document.querySelector(".wr-sub") as HTMLElement;
      return { navRight: n.getBoundingClientRect().right, subLeft: s.getBoundingClientRect().left,
        scroll: n.scrollWidth, client: n.clientWidth };
    });
    expect(box.scroll, language).toBeLessThanOrEqual(box.client + 1);
    expect(box.navRight, language).toBeLessThanOrEqual(box.subLeft + 1);
    // 名稱沒顯示的那幾個，title 仍有名稱
    await expect(nav.locator(".wr-nav__i").nth(10)).toHaveAttribute("title", /\S/);
    // 中文、日文先縮間距與字級就放得下：切到有頁碼的主題後每個主題仍寫名稱（10-23「切換後就變只有數字」）
    if (language !== "en-US") {
      await expect(nav).not.toHaveClass(/compact/);
      await expect(nav.locator(".wr-nav__i").nth(1)).toContainText(/\D{2,}/);
      await expect(nav.locator(".wr-nav__i").nth(10)).toContainText(/AI/);
    }
  }
  // 只放四個主題時每個都寫名稱
  await page.unroute(/\/api\/v1\/warroom\/config$/);
  await pinConfigPages(page, TOPICS.slice(0, 4));
  await page.goto("/warroom");
  for (let i = 0; i < 4; i++) await expect(page.getByTestId("wr-nav").locator(".wr-nav__i").nth(i)).toContainText(/\D{2,}/);
});

async function pinConfigPages(page: Page, pages: string[]) {
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const cfg = await (await route.fetch()).json();
    await route.fulfill({ json: { ...cfg, pages, language: "en-US", reduce_motion: true } });
  });
}
