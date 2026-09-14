/**
 * 幫文件站補畫面截圖 —— 每個畫面拍三種語言，存成 docs/shots/<lang>/<名稱>.png。
 *
 * 為什麼要腳本而不是手動截圖：
 *
 * 1. **英文與日文頁面本來配的是中文截圖**。切了語言，圖裡的字還是中文 ——
 *    那比沒有圖更糟，因為它看起來像「這套系統其實只有中文」。
 * 2. **補拍要拍得出同一個畫面**。手動截圖沒辦法重現：資料變了、視窗大小變了、
 *    滑鼠停在不同的地方，下一版的圖就跟上一版對不起來。
 *
 * 搭配 `scripts/demo_dataset.py`（虛構資料、RFC 5737 文件保留網段）使用：
 * 先灌資料再拍，拍出來的東西不含任何真實環境的資訊。
 *
 * 用法：
 *   cd frontend && node ../scripts/docs-shots.mjs \
 *       --base http://127.0.0.1:5200 --user admin --pass '...' \
 *       --out ../docs/shots
 *
 * 需要 frontend/node_modules 裡的 @playwright/test（`pnpm exec playwright install chromium`
 * 已經裝過瀏覽器）。跑之前先記下既有的 chromium 行程，跑完只關自己開的 ——
 * 這台機器是共用的，別的專案可能正在跑它的 e2e。
 */
import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

// ESM 的 import 是以**這個檔案**的位置找套件的，而 playwright 裝在 frontend/ 底下，
// 所以要明確指到那邊的 node_modules（cd 到 frontend 再跑也沒有用）。
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, "..", "frontend", "package.json"));
const { chromium } = require("@playwright/test");

const args = Object.fromEntries(
  process.argv.slice(2).flatMap((a, i, all) =>
    a.startsWith("--") ? [[a.slice(2), all[i + 1]]] : []),
);
const BASE = args.base || "http://127.0.0.1:5200";
const USER = args.user || "admin";
const PASS = args.pass;
const OUT = args.out || "../docs/shots";
const ONLY = args.only ? new Set(args.only.split(",")) : null;
if (!PASS) {
  console.error("需要 --pass");
  process.exit(2);
}

/** 檔名用的語言代碼 → 後端 user_preferences.locale */
const LANGS = { zh: "zh-TW", en: "en-US", ja: "ja-JP" };

/** 視窗尺寸：16:9，寬到足以讓側邊欄與表格都完整出現 */
const VIEWPORT = { width: 1600, height: 900 };

/**
 * 每張圖：名稱（＝既有檔名）、要去哪裡、拍之前還要做什麼。
 *
 * `prepare` 收到 page，回傳要拍的區域（null＝整個視窗）。刻意不用 fullPage：
 * 文件站的圖是放在版面裡的，過長的圖縮起來誰也看不清楚。
 */
const SHOTS = [
  {
    name: "dashboard",
    async go(page) { await page.goto(`${BASE}/`); await settle(page, 2500); },
  },
  {
    name: "subnet",
    async go(page) {
      await page.goto(`${BASE}/subnets`);
      await settle(page, 1200);
      await page.getByText("198.51.100.0/24", { exact: true }).first().click();
      await settle(page, 2000);
    },
  },
  {
    name: "ip-detail",
    async go(page) {
      const id = await firstAddressId(page);
      await page.goto(`${BASE}/addresses/${id}`);
      await settle(page, 2000);
    },
  },
  {
    name: "topology",
    async go(page) { await page.goto(`${BASE}/topology`); await settle(page, 3000); },
  },
  {
    name: "rack",
    async go(page) {
      await page.goto(`${BASE}/racks`);
      await settle(page, 1500);
      // 選機房而不是單一機櫃：整排並列才看得出這個畫面在做什麼。
      // 用 .n-base-select-option 而不是 getByText —— 後者會先命中頁面上別的地方
      // 同名的字，點下去什麼也不會發生，而且**不會報錯**（拍出來還是預設那一櫃）。
      await page.locator(".n-base-selection").first().click();
      await page.locator(".n-base-select-option", { hasText: "Tokyo DC" })
        .first().click();
      await settle(page, 2500);
      // 確認真的切過去了：預設是照名稱排第一的那一櫃（OSA-R01），沒切到就會拍到它
      const heading = await page.locator("body").innerText();
      if (!heading.includes("TYO-R01")) throw new Error("機房沒有切換成功");
      // 機房平面圖在最上面，機櫃 U 位圖在它下面 —— 不捲的話這張會變成 floorplan.png。
      // 捲到機櫃卡片的標題（「TYO-R01 (42U)」），不要捲到平面圖裡那個同名的小標籤：
      // 那個本來就在畫面上，scrollIntoViewIfNeeded 會什麼都不做。
      await page.getByText("TYO-R01 (42U)").first().scrollIntoViewIfNeeded();
      await page.waitForTimeout(900);
    },
  },
  {
    name: "floorplan",
    async go(page) { await page.goto(`${BASE}/locations`); await settle(page, 2500); },
  },
  {
    name: "device-ports",
    async go(page) {
      const id = await firstDeviceId(page, "core-sw-01");
      await page.goto(`${BASE}/devices/${id}`);
      await settle(page, 2000);
      // 連接埠那一區在基本資料下面 —— 不捲下去的話拍到的是機櫃圖，
      // 跟 rack.png 幾乎一樣，這張就白拍了。捲到某一列（埠名是資料，三種語言
      // 都一樣）而不是捲固定距離：版面一改，固定距離就會落在別的地方。
      await page.locator("tr", { hasText: "Te1/0/8" }).first().scrollIntoViewIfNeeded();
      await page.waitForTimeout(800);
    },
  },
  {
    name: "cable-trace",
    async go(page) {
      // 從 app-srv-01 的 eth0 追出去：中間會穿過跳線盤，才看得出「多跳」的價值
      const id = await firstDeviceId(page, "app-srv-01");
      await page.goto(`${BASE}/devices/${id}`);
      await settle(page, 1500);
      // 連接埠列表的第一個動作按鈕就是追蹤（title 會跟著語言變，所以用位置找）
      const row = page.locator("tr", { hasText: "eth0" }).first();
      await row.locator("button").first().click();
      await settle(page, 2500);
    },
  },
  {
    name: "tools-cidr",
    async go(page) { await page.goto(`${BASE}/tools`); await settle(page, 1500); },
  },
  {
    name: "hostname-arp-sort",
    async go(page) { await page.goto(`${BASE}/hostname-precedence`); await settle(page, 1500); },
  },
  {
    name: "cert-list",
    async go(page) { await page.goto(`${BASE}/certificates`); await settle(page, 1500); },
  },
];

/** 等網路安靜下來再多等一下 —— 圖表與拓樸是畫完才好看的 */
async function settle(page, ms) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(ms);
}

async function api(page, path_, init = {}) {
  return page.evaluate(async ([p, i]) => {
    const r = await fetch(p, {
      ...i,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${localStorage.getItem("access_token")}`,
        ...(i.headers || {}),
      },
    });
    return { status: r.status, body: await r.json().catch(() => null) };
  }, [path_, init]);
}

async function firstAddressId(page) {
  const r = await api(page, "/api/v1/addresses?page_size=100");
  const hit = (r.body?.items || []).find((a) => a.ip === "198.51.100.11")
    || (r.body?.items || [])[0];
  return hit?.id;
}

async function firstDeviceId(page, name) {
  const r = await api(page, "/api/v1/devices?page_size=100");
  const hit = (r.body?.items || []).find((d) => d.name === name)
    || (r.body?.items || [])[0];
  return hit?.id;
}

async function login(page) {
  await page.goto(`${BASE}/login`);
  await page.getByPlaceholder(/帳號|Username|ユーザー名/).fill(USER);
  await page.getByPlaceholder(/密碼|Password|パスワード/).fill(PASS);
  await page.getByRole("button", { name: /^(登入|Sign in|サインイン)$/ }).click();
  await page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 20_000 });
}

async function setLocale(page, locale) {
  const r = await api(page, "/api/v1/me/preferences", {
    method: "PATCH", body: JSON.stringify({ locale }),
  });
  if (r.status !== 200) throw new Error(`切語言失敗 ${locale}: ${r.status}`);
  // 存進 localStorage 才會在重新整理後沿用（未登入畫面也讀這個鍵）
  await page.evaluate((l) => localStorage.setItem("locale", l), locale);
  await page.reload();
  await settle(page, 1200);
}

const browser = await chromium.launch();
try {
  for (const [short, locale] of Object.entries(LANGS)) {
    const dir = path.resolve(OUT, short);
    await mkdir(dir, { recursive: true });
    const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    await login(page);
    await setLocale(page, locale);
    for (const shot of SHOTS) {
      if (ONLY && !ONLY.has(shot.name)) continue;
      try {
        await shot.go(page);
        // 把滑鼠移開再拍：停在圖表上會留下一個 tooltip，看起來像截圖時手滑
        await page.mouse.move(4, 4);
        await page.waitForTimeout(400);
        const file = path.join(dir, `${shot.name}.png`);
        await page.screenshot({ path: file });
        console.log(`${short}/${shot.name}.png`);
      } catch (e) {
        // 一張拍不到不該讓其他的也不拍 —— 但一定要說出是哪一張
        console.error(`  ! ${short}/${shot.name}: ${e.message.split("\n")[0]}`);
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}
