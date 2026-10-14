/**
 * AI 設定：嵌入維度不合要說「模型回幾維、需要幾維」，語意搜尋的索引要看得出需不需要重建
 * （使用者 2026-10-13：換成 4096 維的模型時看到叫人改 EMBEDDING_DIM 的訊息；「看不出來要不要按」重建索引）。
 * 攔截 API，不動真的設定。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function open(page: Page, status: Record<string, unknown>) {
  await page.route("**/api/v1/system/llm", async (route) => {
    const r = await route.fetch();
    const body = await r.json();
    await route.fulfill({ response: r, json: { ...body, enabled: true, url: "http://llm.example.test:11434",
                                                embedding_model: "big-embed:8b" } });
  });
  await page.route("**/api/v1/system/llm/models**", (route) => route.fulfill({ json: { models: [] } }));
  await page.route("**/api/v1/ai/embedding-check", (route) => route.fulfill({ json: {
    ok: false, dim: 4096, expected: 768, error: null } }));
  await page.route("**/api/v1/ai/index-status", (route) => route.fulfill({ json: status }));
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/llm");
}

const BASE = { model: "big-embed:8b", counts: {}, described: 12, missing: 0 };

test("維度不合講清楚；換過模型提示要重建", async ({ page }) => {
  await open(page, { ...BASE, needs_reindex: true, reason: "model_changed",
                     last: { model: "granite-embedding:278m", dim: 768, at: "2026-10-01T00:00:00Z", indexed: 12, failed: 0 } });
  await expect(page.getByText(/模型回 4096 維，資料庫欄位是 768 維/)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/EMBEDDING_DIM/)).toHaveCount(0);
  const st = page.getByTestId("llm-index-status");
  await expect(st).toContainText("建議重建");
  await expect(st).toContainText("granite-embedding:278m");
  await expect(st).toContainText("big-embed:8b");
});

test("索引是最新的", async ({ page }) => {
  await open(page, { ...BASE, needs_reindex: false, reason: null,
                     last: { model: "big-embed:8b", dim: 768, at: "2026-10-13T08:00:00Z", indexed: 12, failed: 0 } });
  await expect(page.getByTestId("llm-index-status")).toContainText("索引是最新的", { timeout: 20_000 });
});

test("有資料缺向量", async ({ page }) => {
  await open(page, { ...BASE, needs_reindex: true, reason: "missing", missing: 3,
                     last: { model: "big-embed:8b", dim: 768, at: "2026-10-13T08:00:00Z", indexed: 9, failed: 0 } });
  await expect(page.getByTestId("llm-index-status")).toContainText("3 筆有說明但沒有向量", { timeout: 20_000 });
});
