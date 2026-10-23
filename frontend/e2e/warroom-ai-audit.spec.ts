/**
 * 戰情室 11 AI 巡檢結果（2026-10-23）：版面不溢出（1920×1080 與 3840×2160、中英文）、右側焦點每 10 秒換一筆、
 * 整個戰情室暫停時焦點也停住；未執行與失敗不會看起來像「沒有發現」。
 * 頁面資料用攔截的固定內容（形狀與 backend/app/services/warroom/ai_audit.py 回的一致）：共用的後端不一定有這一版的程式，
 * 也不一定有巡檢結果。資料正確性與權限在 backend/tests/test_warroom_ai_audit.py。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const SHOTS = process.env.E2E_SHOT_DIR || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");
test.use({ viewport: { width: 1920, height: 1080 } });

const TOPICS = ["overview", "anomalies", "ai_audit"];

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

/** 播放清單含 ai_audit、指定語言；停留時間拉長，測焦點輪播時不會剛好換主題（不動伺服器設定） */
async function pinConfig(page: Page, language = "", reduce = true) {
  await page.route(/\/api\/v1\/warroom\/config$/, async (route) => {
    const res = await route.fetch();
    const cfg = await res.json();
    await route.fulfill({ json: { ...cfg, pages: TOPICS, language, reduce_motion: reduce, dwell_seconds: 600 } });
  });
}

const H = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();

type Lang = "zh" | "en";
const TEXT: Record<Lang, { title: string[]; detail: string; rec: string; note: string }> = {
  zh: {
    title: ["同一 IP 出現不同 MAC，ARP 與 DHCP 租約回報的網卡位址不一致", "DNS 名稱與 IPAM 登記不一致",
            "長期未觀察到的保留位址", "管理介面位於一般使用者網段", "疑似測試設備在正式環境網段"],
    detail: "ARP 回報的 MAC 與 DHCP 租約上的 MAC 不同；可能是更換過網卡、租約尚未清除，或兩台裝置使用同一位址，需要交叉核對租約時間與交換器連接埠。",
    rec: "請確認這個位址最近是否更換過網卡或主機，並核對 DHCP 租約的到期時間；若兩個 MAC 同時出現在交換器上，再檢查是否有位址衝突。",
    note: "ARP 與 DHCP 的 MAC 不同",
  },
  en: {
    title: ["Same IP reported with two different MAC addresses by ARP and the DHCP lease", "DNS name does not match the IPAM record",
            "Reserved address not seen for a long time", "Management interface in a general user subnet", "Possible test device in a production range"],
    detail: "The MAC reported by ARP differs from the one on the DHCP lease. This may be a replaced NIC, a lease not yet cleared, or two devices sharing the address; cross-check the lease times and switch ports.",
    rec: "Check whether the NIC or host behind this address was replaced recently and compare the DHCP lease expiry; if both MACs appear on the switch, look for an address conflict.",
    note: "ARP and DHCP report different MACs",
  },
};

function item(lang: Lang, i: number) {
  const tx = TEXT[lang];
  const subjects = [
    [{ kind: "ip", ip: "198.51.100.58", hostname: "finance-application-server-02.corp.tenant-a.example.com", verified: true }],
    [{ kind: "ip", ip: "10.20.30.41", hostname: "mail-gateway-taipei-primary.internal.example.com", verified: true },
     { kind: "device", name: "core-switch-taipei-01", verified: true }],
    [{ kind: "ip", ip: "2001:db8:abcd:12:0:0:0:1a2b", hostname: null, verified: true },
     { kind: "unverified", text: "172.16.250.199", verified: false }],
    [{ kind: "ip", ip: "203.0.113.35", hostname: "idrac-r740-03", verified: true }],
    [{ kind: "subnet", cidr: "10.99.0.0/24", verified: true }],
  ][i];
  const facts = [
    [{ at: H(1.2), source: "arp", ip: "198.51.100.58", kind: "last_seen" },
     { at: H(2.5), source: "scanner", ip: "198.51.100.58", kind: "last_seen" },
     { at: H(1.2), source: "arp", ip: "198.51.100.58", kind: "mac", value: "00:11:22:33:44:55" }],
    [{ at: H(26), source: "dns", ip: "10.20.30.41", kind: "last_seen" },
     { at: H(30), source: "librenms", ip: "10.20.30.41", kind: "last_seen" }],
    [{ at: null, source: "ipam", ip: "2001:db8:abcd:12:0:0:0:1a2b", kind: "never_seen" }],
    [{ at: H(0.5), source: "scanner", ip: "203.0.113.35", kind: "last_seen" }],
    [],
  ][i];
  return {
    id: `f-${i}`, rank: i + 1, priority: (["high", "medium", "low", "high", "medium"] as const)[i],
    category: ["conflict", "naming", "stale", "exposure", "policy"][i], review: "open", title: tx.title[i],
    subjects, more_subjects: 0, unverified: subjects.filter((s) => !s.verified).length,
    sources: [["arp", "scanner", "dhcp"], ["dns", "librenms"], [], ["scanner"], ["ipam"]][i],
    facts, detail: tx.detail, note: tx.note, recommendation: i === 4 ? null : tx.rec,
    found_at: H(30 + i), seen_at: H(1), model: "gemma4:26b",
  };
}

function payload(lang: Lang, page = 1, over: Record<string, unknown> = {}) {
  const all = [0, 1, 2, 3, 4].map((i) => item(lang, i));
  const items = all.slice((page - 1) * 3, page * 3);
  return {
    key: "ai_audit", data_at: new Date().toISOString(), page, pages: 2, per_page: 3, total: 5,
    metrics: { high: 2, open: 5, insufficient: 1 },
    run: {
      status: "completed", running_since: null, error_code: null, last_attempt_at: H(2),
      completed_at: H(2), completed_status: "completed", snapshot_at: H(2.2), freshness: "fresh",
      scope: { subnets: 25, ips: 400, capped: true },
      schedule: { enabled: true, next_at: new Date(Date.now() + 20 * 3600_000).toISOString(), due_now: false },
    },
    findings: { state: "ok", items },
    withheld: null, dismissed: 2,
    gaps: [{ code: "sample_cap", n: 400 }, { code: "not_included", n: 3 }],
    ...over,
  };
}

async function mockPage(page: Page, lang: Lang, over: Record<string, unknown> = {}) {
  await page.route(/\/api\/v1\/warroom\/pages\/ai_audit/, async (route) => {
    const p = Number(new URL(route.request().url()).searchParams.get("page") || "1");
    await route.fulfill({ json: payload(lang, Math.min(p, 2), over) });
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

/** 區塊內容有沒有被自己的框裁掉（版面區塊 overflow:hidden 時 scrollHeight 會大於 clientHeight） */
async function clipped(page: Page): Promise<string[]> {
  return page.evaluate(() => [".aa-list", ".aa-focus", ".aa-body", ".wr-main"].flatMap((s) =>
    [...document.querySelectorAll<HTMLElement>(s)].filter((e) => e.scrollHeight > e.clientHeight + 2)
      .map((e) => `${s} ${e.scrollHeight}>${e.clientHeight}`)));
}

async function open(page: Page) {
  await page.goto("/warroom");
  await expect(page.locator(".wr-title h1")).toBeVisible({ timeout: 20_000 });
  await page.keyboard.press("3");
  await expect(page.getByTestId("wr-ai")).toBeVisible();
}

for (const [lang, code] of [["zh", "zh-TW"], ["en", "en-US"], ["ja", "ja-JP"]] as const) {
  for (const [w, h] of (lang === "ja" ? [[1920, 1080]] : [[1920, 1080], [3840, 2160]]) as [number, number][]) {
    test(`版面不溢出（${code} ${w}×${h}）`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.setViewportSize({ width: w, height: h });
      await pinConfig(page, code);
      await mockPage(page, lang === "ja" ? "zh" : lang);      // 模型寫的內容沿用中文；畫面固定字串是日文
      await login(page);
      await open(page);
      await expect(page.getByTestId("wr-ai-item")).toHaveCount(3);
      expect(await overflowing(page)).toEqual([]);
      expect(await clipped(page)).toEqual([]);
      // IP 不截斷：整個 IP 都在畫面上（沒有 text-overflow 吃掉）
      const ipCut = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".aa-ip")]
        .filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.textContent));
      expect(ipCut).toEqual([]);
      // 三個指標旁邊寫明不能相加；不出現百分比可信度
      await expect(page.getByTestId("wr-ai-run")).toContainText({ zh: "不可相加", en: "do not add", ja: "合計不可" }[lang]);
      // 次列的時間與涵蓋範圍不被省略號吃掉（只有新鮮度說明可以縮）
      const runCut = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".aa-run__i:not(.aa-run__fresh), .aa-status")]
        .filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.textContent));
      expect(runCut).toEqual([]);
      expect(await page.getByTestId("wr-ai").innerText()).not.toMatch(/\d+\s*%/);
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/ai-audit-${lang}-${w}.png` });
      // 第二頁：最後兩筆（含沒有建議的那一筆）
      await page.keyboard.press("ArrowDown");
      await expect(page.getByTestId("wr-ai-item")).toHaveCount(2);
      expect(await overflowing(page)).toEqual([]);
      if (SHOTS && w === 1920) await page.screenshot({ path: `${SHOTS}/ai-audit-${lang}-${w}-p2.png` });
      expect(errors).toEqual([]);
    });
  }
}

test("右側焦點每 10 秒換一筆；整個戰情室暫停時停住", async ({ page }) => {
  await pinConfig(page, "zh-TW");
  await mockPage(page, "zh");
  await page.clock.install();                         // 要在開頁面之前裝：載入時就設好的計時器才快轉得到
  await login(page);
  await open(page);
  const focus = page.getByTestId("wr-ai-focus-item");
  const items = page.getByTestId("wr-ai-item");
  await expect(focus).toHaveAttribute("data-id", "f-0");
  await expect(items.nth(0)).toHaveAttribute("data-focus", "1");
  await page.clock.fastForward(10_500);
  await expect(focus).toHaveAttribute("data-id", "f-1");
  await expect(items.nth(1)).toHaveAttribute("data-focus", "1");
  await expect(page.getByTestId("wr-ai-focus-pos")).toContainText("2／3");
  // 暫停：再過 25 秒也不換
  await page.keyboard.press(" ");
  await expect(page.locator(".wr-paused")).toBeVisible();
  await page.clock.fastForward(25_000);
  await expect(focus).toHaveAttribute("data-id", "f-1");
  // 繼續：10 秒後換下一筆，最後一筆之後回到第一筆
  await page.keyboard.press(" ");
  await expect(page.locator(".wr-paused")).toHaveCount(0);
  await page.clock.fastForward(10_500);
  await expect(focus).toHaveAttribute("data-id", "f-2");
  await page.clock.fastForward(10_500);
  await expect(focus).toHaveAttribute("data-id", "f-0");
  // 點一下清單裡的某一筆也可以直接聚焦（電視上不需要，操作時方便）
  await items.nth(2).click();
  await expect(focus).toHaveAttribute("data-id", "f-2");
});

test("未執行、失敗、進行中、不能顯示：都不會看起來像「沒有發現」", async ({ page }) => {
  await pinConfig(page, "zh-TW");
  let over: Record<string, unknown> = {};
  await page.route(/\/api\/v1\/warroom\/pages\/ai_audit/, async (route) => {
    await route.fulfill({ json: payload("zh", 1, over) });
  });
  const dash = { high: "—", open: "—", insufficient: "—" };
  const baseRun = payload("zh").run;
  const cases: [string, Record<string, unknown>, RegExp, RegExp][] = [
    ["not_run", { metrics: dash, total: 0, pages: 1, findings: { state: "unavailable", items: [] },
      run: { ...baseRun, status: "not_run", completed_at: null, completed_status: null, snapshot_at: null, freshness: null,
             last_attempt_at: null, scope: { subnets: null, ips: null, capped: false },
             schedule: { enabled: false, next_at: null, due_now: false } } }, /尚未執行過/, /未設定定期巡檢/],
    ["failed", { metrics: dash, total: 0, pages: 1, findings: { state: "unavailable", items: [] },
      run: { ...baseRun, status: "failed", error_code: "llm_error", completed_at: null, completed_status: null,
             snapshot_at: null, freshness: null, scope: { subnets: null, ips: null, capped: false } } }, /巡檢失敗，沒有可顯示/, /下次排程/],
    ["running", { run: { ...baseRun, status: "running", running_since: H(0.1) } }, /顯示 .* 完成的結果/, /下次排程/],
    ["withheld", { metrics: dash, total: 0, pages: 1, findings: { state: "withheld", items: [] }, withheld: { count: null },
      run: { ...baseRun, scope: { subnets: null, ips: null, capped: false } } }, /可見範圍以外/, /下次排程/],
  ];
  await login(page);
  for (const [name, o, body, foot] of cases) {
    over = o;
    await open(page);
    await expect(page.getByTestId("wr-ai-run"), name).toHaveAttribute("data-status", String((o.run as { status?: string } | undefined)?.status ?? "completed"));
    await expect(page.getByTestId("wr-ai"), name).toContainText(body);
    await expect(page.getByTestId("wr-ai-foot"), name).toContainText(foot);
    await expect(page.getByTestId("wr-ai"), name).not.toContainText("這次巡檢沒有發現");
    if (name !== "running") {
      // 三個指標都寫「無結果」（小字），不是 0、也不是一條大橫線
      await expect(page.getByTestId("wr-metric-none"), name).toHaveCount(3);
      await expect(page.locator(".wr-metric__v"), name).toHaveCount(0);
    }
    expect(await overflowing(page), name).toEqual([]);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/ai-audit-state-${name}.png` });
  }
});
