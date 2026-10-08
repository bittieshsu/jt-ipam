/**
 * IP 變更評估：開關、從 IP 頁建立、從清單頁先選子網路再輸入 IP、看結果、匯出、送審與覆核的權限關卡。
 *
 * 打真的後端（seed_e2e 的 198.51.100.7 有防火牆別名、規則與 NAT 引用）。功能預設關閉，
 * 這支測試開了之後在 afterAll 關回去，不影響其他測試的選單。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const SAMPLE_IP = "198.51.100.7";

test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function token(request: APIRequestContext): Promise<string> {
  const r = await request.post("/api/v1/auth/login", { data: { username: ADMIN_USER, password: ADMIN_PASS, realm: "local" } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).access_token;
}

async function login(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
}

let ipId = "";
let wasEnabled = false;

test.beforeAll(async ({ request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  wasEnabled = (await (await request.get("/api/v1/change-impact/settings", { headers: auth })).json()).enabled;
  const found = await (await request.get(`/api/v1/addresses?q=${SAMPLE_IP}&page_size=5`, { headers: auth })).json();
  ipId = found.items?.find((x: { ip: string }) => String(x.ip).split("/")[0] === SAMPLE_IP)?.id ?? "";
  expect(ipId, "找不到樣本 IP（先跑 seed_e2e）").not.toBe("");
});

test.afterAll(async ({ request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await request.put("/api/v1/change-impact/settings", { headers: auth, data: { enabled: wasEnabled } });
});

test("關閉時沒有入口；在系統設定打開後，選單與 IP 頁都出現", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await request.put("/api/v1/change-impact/settings", { headers: auth, data: { enabled: false } });
  await login(page);
  await page.goto(`/addresses/${ipId}`);
  await expect(page.getByRole("button", { name: "調查" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("ip-change-impact-btn")).toHaveCount(0);
  await page.goto("/system-settings");
  const sw = page.getByTestId("cip-enabled-switch");
  await expect(sw).toBeVisible({ timeout: 20_000 });
  await sw.click();
  await expect(page.locator(".n-menu").getByText("IP 變更評估")).toBeVisible();
  await page.goto(`/addresses/${ipId}`);
  await expect(page.getByTestId("ip-change-impact-btn")).toBeVisible({ timeout: 20_000 });
});

test("從 IP 頁評估改址：看到引用、證據、缺口與模板待辦，可以匯出；送審後的覆核要逐項處置並填理由", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await request.put("/api/v1/change-impact/settings", { headers: auth, data: { enabled: true, allow_self_review: false } });
  await login(page);
  await page.goto(`/addresses/${ipId}`);
  await page.getByTestId("ip-change-impact-btn").click();
  await expect(page.getByTestId("cip-wiz-target")).toHaveText(SAMPLE_IP);
  await page.getByTestId("cip-wiz-new-ip").locator("input").fill("198.51.100.77");
  await page.getByTestId("cip-wiz-submit").click();
  await expect(page).toHaveURL(/\/change-impact\//, { timeout: 20_000 });
  await expect(page.getByTestId("cip-dryrun")).toContainText("尚未執行任何變更");
  await expect(page.getByTestId("cip-summary")).toBeVisible({ timeout: 30_000 });
  const findings = page.getByTestId("cip-findings");
  await expect(findings).toContainText("web_hosts");
  await expect(findings).toContainText("e2e-https-forward");
  // 展開一列：看得到證據的來源與收錄時間、規則版本
  await page.locator(".n-data-table-expand-trigger").first().click();
  await expect(findings).toContainText("v1");
  await page.locator(".n-tabs-tab", { hasText: "證據與資料不足" }).click();
  await expect(page.getByTestId("cip-gaps")).toContainText("來源或目的為 any 的規則不逐條列出");
  await page.locator(".n-tabs-tab", { hasText: "待辦與復原" }).click();
  await expect(page.getByTestId("cip-phase-rollback")).toContainText("198.51.100.7");
  await expect(page.getByTestId("cip-phase-change")).toContainText("防火牆");
  // 關係圖（2026-10-08 規劃人員建議）：節點不多預設樹狀；圖例收在按鈕裡、講清楚箭頭方向；
  // 點評估目標開旁邊的明細欄，標「評估目標」、關係依類型分組
  await page.locator(".n-tabs-tab", { hasText: "關係圖" }).click();
  const canvas = page.locator(".irg-canvas");
  await expect(canvas).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(800);
  // 預設是分層（使用者 2026-10-08 指定），每一圈有標題與數量
  await expect(page.getByTestId("cip-graph-layout-rings")).toHaveClass(/checked/);
  const zones = await page.evaluate(() =>
    (document.querySelector(".irg-canvas") as any)?._cyreg?.cy?.nodes("[zone = 1]").length ?? 0);
  expect(zones).toBeGreaterThan(0);
  await page.getByTestId("cip-graph-legend-btn").click();
  await expect(page.getByTestId("cip-graph-legend")).toContainText("箭頭由引用的一方指向被引用的物件");
  await page.getByTestId("cip-graph-legend-btn").click();
  await expect(page.getByTestId("cip-graph-legend")).toHaveCount(0);
  await canvas.scrollIntoViewIfNeeded();
  const rootPos = await page.evaluate(() => {
    const cy = (document.querySelector(".irg-canvas") as any)._cyreg.cy;
    const p = cy.nodes("[root = 1]")[0].renderedPosition();
    return { x: p.x, y: p.y };
  });
  const cbox = (await canvas.boundingBox())!;
  await page.mouse.click(cbox.x + rootPos.x, cbox.y + rootPos.y);
  await expect(page.getByTestId("cip-graph-picked")).toContainText("評估目標");
  await expect(page.getByTestId("cip-graph-rel-group").first()).toBeVisible();
  await page.getByTestId("cip-graph-close").click();
  await expect(page.getByTestId("cip-graph-picked")).toHaveCount(0);
  // 匯出 SVG（使用者 2026-10-08）：向量圖檔
  await page.getByTestId("cip-graph-export").hover();
  const [svgDl] = await Promise.all([page.waitForEvent("download"), page.getByText("SVG（向量圖，可編輯）").click()]);
  expect(svgDl.suggestedFilename()).toMatch(/\.svg$/);
  // 匯出 Markdown
  await page.getByTestId("cip-export").hover();
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByText("Markdown", { exact: true }).click()]);
  expect(dl.suggestedFilename()).toMatch(/\.md$/);
  // 送審 → 覆核：沒填處置與理由就送不出去
  // 送審先跳確認視窗、列出會通知誰；按確認才真的送出
  await page.getByTestId("cip-act-submit").click();
  const confirm = page.getByTestId("cip-submit-confirm");
  await expect(confirm).toBeVisible();
  await expect(confirm.getByTestId("cip-submit-reviewers")).toBeVisible();
  await expect(page.getByTestId("cip-lifecycle")).toHaveText("草稿");
  await confirm.getByTestId("cip-submit-confirm-ok").click();
  await expect(page.getByTestId("cip-lifecycle")).toHaveText("送審中");
  await page.getByTestId("cip-act-review").click();
  await page.getByTestId("cip-review-submit").click();
  await expect(page.locator(".n-message")).toContainText(/不能覆核自己的計畫|需要處置|reason|理由/);
});

test("從清單頁新增：先選子網路（輸入 IP 找包含它的子網路），IP 自動帶入；按鈕都有圖示", async ({ page, request }) => {
  const auth = { Authorization: `Bearer ${await token(request)}` };
  await request.put("/api/v1/change-impact/settings", { headers: auth, data: { enabled: true } });
  await login(page);
  await page.goto("/change-impact");
  await page.getByTestId("cip-new").click();
  const wiz = page.getByTestId("cip-wizard");
  await expect(wiz).toBeVisible();
  // 還沒選子網路：IP 欄位不能填
  await expect(page.getByTestId("cip-wiz-old-ip").locator("input")).toBeDisabled();
  await page.getByTestId("cip-wiz-subnet-pick").click();
  await page.getByTestId("cip-wiz-subnet-pick").locator("input").fill(SAMPLE_IP);
  const opt = page.locator(".n-base-select-option").filter({ hasText: "198.51.100.0/" }).first();
  await expect(opt).toBeVisible({ timeout: 10_000 });
  await opt.click();
  await expect(page.getByTestId("cip-wiz-old-ip").locator("input")).toHaveValue(SAMPLE_IP);
  await page.getByTestId("cip-wiz-new-ip").locator("input").fill("198.51.100.78");
  await expect(page.getByTestId("cip-wiz-submit")).toBeEnabled({ timeout: 10_000 });
  // 對話框底部兩顆按鈕都有圖示
  await expect(wiz.locator(".n-card__footer .n-button .n-button__icon")).toHaveCount(2);
});

