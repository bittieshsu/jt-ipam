/**
 * 「裝置關聯可能過期」多了「原因」欄（2026-10-14）：
 *   - 關聯之後 MAC 換過（原本的依據）
 *   - 裝置在 LibreNMS 已停用／離線，而位址上的 MAC 廠牌不是那台裝置的廠牌（新的依據）
 * 後端送 `reason_key`＋`reason_params`，畫面翻成句子；{mac_vendor}／{device_vendor} 要代入，不能原樣露出。
 * 偵測結果用路由攔截（e2e 環境沒有 LibreNMS）。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

const EMPTY: Record<string, unknown[]> = {
  ip_conflicts: [], mac_drifts: [], ghost_ips: [], unauthorized_ips: [], rogue_dhcp: [],
  external_exposure: [], dangling_dns: [], duplicate_ip_records: [], suspicious_changes: [],
  fw_rule_rot: [], arp_only_liveness: [], mac_flapping: [],
};
const ROWS = [
  { kind: "stale_device_link", ip_id: "00000000-0000-4000-8000-000000000a01", ip: "198.51.100.190",
    hostname: "tv-01", mac: "00:00:5e:00:53:90", device_id: "00000000-0000-4000-8000-000000000d01",
    device: "ap-e2e", mac_vendor: "SamsungElect", device_vendor: "Ubiquiti", librenms_status: "disabled",
    linked_at: null, mac_changed_at: null,
    reason_key: "anomaly.stale_reason_vendor_disabled",
    reason_params: { mac_vendor: "SamsungElect", device_vendor: "Ubiquiti" } },
  { kind: "stale_device_link", ip_id: "00000000-0000-4000-8000-000000000a02", ip: "198.51.100.20",
    hostname: "printer", mac: "00:00:5e:00:53:20", device_id: "00000000-0000-4000-8000-000000000d02",
    device: "srv-e2e", linked_at: "2026-10-01T00:00:00Z", mac_changed_at: "2026-10-10T00:00:00Z",
    reason_key: "anomaly.stale_reason_mac_changed", reason_params: {} },
];

async function open(page: Page) {
  await page.route("**/api/v1/anomalies/scan", (route) =>
    route.fulfill({ json: { ...EMPTY, stale_device_links: ROWS } }));
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await page.goto("/anomaly?tab=stale_device_links");
  await page.getByRole("button", { name: /執行偵測/ }).first().click();
  const table = page.locator(".n-data-table").first();
  await expect(table).toContainText("tv-01", { timeout: 20_000 });
  return table;
}

test("原因欄：停用＋廠牌對不上、MAC 換過，兩種都翻成句子", async ({ page }) => {
  const table = await open(page);
  const heads = (await table.locator("th").allInnerTexts()).join(" | ");
  expect(heads).toContain("原因");
  const body = await table.innerText();
  expect(body).toContain("裝置在 LibreNMS 已停用，MAC 是 SamsungElect 的，不是 Ubiquiti");
  expect(body).toContain("關聯之後 MAC 換過");
  expect(body, "參數沒代入").not.toMatch(/\{mac_vendor\}|\{device_vendor\}/);
  expect(body, "不能把翻譯鍵原樣印出來").not.toContain("stale_reason");
});
