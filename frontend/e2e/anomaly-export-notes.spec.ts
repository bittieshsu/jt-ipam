/**
 * 異常偵測（使用者 2026-10-13）：
 *   - 「偵測範圍」「排程」是有框線的按鈕（以前是無框的文字按鈕）
 *   - 分類說明預設只有一句話摘要，「詳細說明」展開完整內容；展開與否記住（換頁籤、重新整理都一樣）
 *   - 匯出：整份報告 PDF／Word／ODT／HTML、工作表 XLSX／ODS、目前分類 CSV；照畫面的欄位與篩選
 * scan 端點攔截：驗 UI 與檔案內容，不驗偵測本身。
 */
import { readFileSync } from "node:fs";
import { test, expect, type Download, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

const REPORT = {
  ip_conflicts: [{
    ip: "198.51.100.7", evidence: ["arp"], confidence: "high",
    macs: [{ mac: "02:00:00:00:00:01", vendor: "ExampleCorp", sources: ["scanner"] },
           { mac: "02:00:00:00:00:02", local: true }],
  }],
  mac_drifts: [], rogue_dhcp: [], external_exposure: [], dangling_dns: [],
  duplicate_ip_records: [], suspicious_changes: [], fw_rule_rot: [],
  // 主機名稱是外部可控的文字：匯出的 HTML 要跳脫
  ghost_ips: [{ ip: "198.51.100.21", hostname: "<b>pc-fin</b>" }],
  unauthorized_ips: [{ ip: "198.51.100.22" }], unauthorized_total: 1,
  // 可以逐 IP 忽略的類別：操作欄有「忽略這個 IP」
  mac_flapping: [{ ip: "198.51.100.40", ip_id: "00000000-0000-4000-8000-000000000040", hostname: "laptop-07",
                   mac_count: 3, randomized: true, days: 7,
                   macs: [{ mac: "02:00:00:00:00:41" }, { mac: "02:00:00:00:00:42" }, { mac: "02:00:00:00:00:43" }] }],
};

async function open(page: Page) {
  await page.route("**/api/v1/anomalies/scan", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(REPORT) }));
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/anomaly?tab=ip_conflicts");
  await page.getByRole("button", { name: /執行偵測/ }).click();
  await expect(page.locator(".n-tabs-tab", { hasText: /^IP 衝突 \(1\)$/ })).toBeVisible();
}

async function exportAs(page: Page, label: string): Promise<{ dl: Download; body: Buffer }> {
  await page.getByTestId("anomaly-export").click();
  const [dl] = await Promise.all([page.waitForEvent("download"),
                                  page.locator(".n-dropdown-option", { hasText: label }).first().click()]);
  return { dl, body: readFileSync((await dl.path())!) };
}

test("偵測範圍／排程有框線；說明預設一句話、可展開且會記住", async ({ page }) => {
  await open(page);
  for (const id of ["anomaly-scope-btn", "anomaly-sched-btn"]) {
    const border = await page.getByTestId(id).evaluate((el) => {
      const b = el.querySelector(".n-button__border");
      return b ? getComputedStyle(b).borderTopWidth : "none";
    });
    expect(border, id).not.toBe("none");
    expect(parseFloat(border), id).toBeGreaterThan(0);
  }

  const pane = page.locator(".n-tab-pane:visible");
  await expect(pane.getByTestId("cat-summary")).toHaveText("同一個 IP 被兩台以上的機器使用（不同 MAC 搶同一個位址）。");
  await expect(pane.getByTestId("cat-explain")).toHaveCount(0);
  await pane.getByTestId("cat-explain-toggle").click();
  await expect(pane.getByTestId("cat-explain")).toContainText("兩種依據");
  await expect(pane.getByTestId("cat-explain-toggle")).toHaveAttribute("aria-expanded", "true");

  // 換頁籤：同樣展開
  await page.locator(".n-tabs-tab", { hasText: /^失聯 IP/ }).click();
  await expect(page.locator(".n-tab-pane:visible")).toHaveCount(1);
  await expect(page.locator(".n-tab-pane:visible").getByTestId("cat-explain")).toBeVisible();
  // 重新整理：還記得
  await page.reload();
  await page.getByRole("button", { name: /執行偵測/ }).click();
  await expect(page.locator(".n-tab-pane:visible").getByTestId("cat-explain")).toBeVisible();
  // 收起
  await page.locator(".n-tab-pane:visible").getByTestId("cat-explain-toggle").click();
  await expect(page.locator(".n-tab-pane:visible").getByTestId("cat-explain")).toHaveCount(0);

  // 「忽略這個 IP」要有圖示：視窗窄時操作欄只剩圖示，沒有圖示就是一個空白的灰色塊
  await page.locator(".n-tabs-tab", { hasText: /^IP 頻繁更換 MAC/ }).click();
  await expect(page.locator(".n-tab-pane:visible")).toHaveCount(1);
  const ignore = page.locator(".n-tab-pane:visible td.col-actions button", { hasText: "忽略這個 IP" });
  await expect(ignore).toHaveCount(1);
  await expect(ignore.locator("svg")).toHaveCount(1);
});

test("匯出：HTML／Word／ODT／PDF 報告、XLSX／ODS 工作表、CSV 目前分類；照篩選", async ({ page }) => {
  await open(page);

  const html = await exportAs(page, "HTML");
  expect(html.dl.suggestedFilename()).toMatch(/^anomaly-report-\d{8}-\d{4}\.html$/);
  const text = html.body.toString("utf8");
  expect(text).toContain("<h1>異常偵測報告</h1>");
  expect(text).toContain("<h2>總覽</h2>");
  expect(text).toContain("<h2>IP 衝突 (1)</h2>");
  expect(text).toContain("02:00:00:00:00:01（ExampleCorp）");
  expect(text).toContain("&lt;b&gt;pc-fin&lt;/b&gt;");
  expect(text).not.toContain("<b>pc-fin</b>");
  // 沒資料的分類只在總覽出現，不另外印一個空表格
  expect(text).not.toContain("<h2>MAC 變動");

  for (const [label, ext] of [["Word", "docx"], ["OpenDocument (.odt)", "odt"], ["Excel", "xlsx"], ["OpenDocument 試算表", "ods"]]) {
    const f = await exportAs(page, label);
    expect(f.dl.suggestedFilename(), label).toMatch(new RegExp(`\\.${ext}$`));
    expect(f.body.subarray(0, 2).toString("latin1"), label).toBe("PK");
  }

  const pdf = await exportAs(page, "PDF");
  expect(pdf.dl.suggestedFilename()).toMatch(/\.pdf$/);
  expect(pdf.body.subarray(0, 4).toString("latin1")).toBe("%PDF");

  // CSV：目前的分類（IP 衝突），多個 MAC 一個一行
  const csv = await exportAs(page, "CSV");
  expect(csv.dl.suggestedFilename()).toMatch(/-ip_conflicts\.csv$/);
  const csvText = csv.body.toString("utf8");
  expect(csvText).toContain("198.51.100.7");
  expect(csvText).toMatch(/02:00:00:00:00:01（ExampleCorp）[^"]*\n02:00:00:00:00:02/);

  // 篩選：報告寫出篩選條件，總覽是「符合／全部」
  await page.locator(".n-tab-pane:visible .cat-filter input").fill("198.51.100.22");
  const filtered = (await exportAs(page, "HTML")).body.toString("utf8");
  expect(filtered).toContain("198.51.100.22");
  expect(filtered).toContain("<td>0/1</td>");
  expect(filtered).not.toContain("<h2>IP 衝突");
});
