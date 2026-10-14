/**
 * LibreNMS 整合：VLAN 同步寫進哪個 domain（GitHub issue #53）。
 *   - 預設（不選）＝專用的 LibreNMS domain
 *   - 選了 domain 才出現「只對應既有 VLAN、不新建」
 *   - 儲存時送 vlan_domain_id／vlan_create_missing；清掉 domain 送 null（改回專用 domain）
 * 攔截 API，不建真的整合。
 */
import { test, expect, type Page } from "@playwright/test";

const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

const INST = {
  id: "00000000-0000-4000-8000-0000000000aa", name: "lnms-e2e", api_url: "https://librenms.example",
  enabled: true, verify_tls: true, sync_devices: true, sync_arp: true, sync_fdb: true, sync_vlans: true,
  sync_links: true, use_for_status: true, auto_add_devices: true, auto_create_ips: true,
  auto_create_from_arp: false, arp_create_require_fdb: true, arp_create_skip_dhcp: true,
  sync_interval_seconds: 300, scope_subnet_ids: [], vlan_domain_id: null, vlan_create_missing: true,
  last_sync_at: null, last_error: null, created_at: "2026-10-13T00:00:00Z", updated_at: "2026-10-13T00:00:00Z",
};

async function open(page: Page) {
  const patches: any[] = [];
  await page.route("**/api/v1/librenms/instances**", async (route) => {
    if (route.request().method() === "PATCH") {
      patches.push(route.request().postDataJSON());
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...INST, ...route.request().postDataJSON() }) });
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [INST], total: 1, page: 1, page_size: 50 }) });
  });
  await page.route("**/api/v1/vlan-domains**", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    items: [{ id: "00000000-0000-4000-8000-0000000000d1", name: "default", description: null }], total: 1, page: 1, page_size: 200 }) }));
  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill("admin");
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto("/librenms");
  await expect(page.getByText("lnms-e2e").first()).toBeVisible({ timeout: 20_000 });
  return patches;
}

test("選 VLAN domain、只對應既有、清回預設", async ({ page }) => {
  const patches = await open(page);
  await page.getByRole("button", { name: /編輯/ }).first().click();
  const box = page.getByTestId("lnms-vlan-domain");
  await expect(box).toBeVisible();
  await expect(box.getByTestId("lnms-vlan-only-existing")).toHaveCount(0);   // 沒選 domain 時不出現

  await box.getByTestId("lnms-vlan-domain-select").click();
  await page.locator(".n-base-select-option", { hasText: /^default$/ }).click();
  await box.getByTestId("lnms-vlan-only-existing").click();
  const save1 = page.waitForRequest((r) => r.method() === "PATCH" && r.url().includes("/librenms/instances/"));
  await page.getByRole("button", { name: /儲存/ }).last().click();
  await save1;
  expect(patches.at(-1)).toMatchObject({ vlan_domain_id: "00000000-0000-4000-8000-0000000000d1", vlan_create_missing: false });

  // 清掉 domain：送 null（改回專用的 LibreNMS domain）
  await page.getByRole("button", { name: /編輯/ }).first().click();
  const sel = page.getByTestId("lnms-vlan-domain-select");
  await sel.hover();
  await sel.locator(".n-base-clear").click();
  const save2 = page.waitForRequest((r) => r.method() === "PATCH" && r.url().includes("/librenms/instances/"));
  await page.getByRole("button", { name: /儲存/ }).last().click();
  await save2;
  expect(patches.at(-1)).toHaveProperty("vlan_domain_id", null);
});
