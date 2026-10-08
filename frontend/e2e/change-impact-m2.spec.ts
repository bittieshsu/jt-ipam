/**
 * IP 變更評估 M2：服務登錄、交換器維護評估（從裝置頁的「變更評估」進入）。
 *
 * 用 API 建一個小拓樸：app 只接 sw（模型內中斷）、db 另外接 sw2（備援待驗證）、服務 ERP 依賴 app。
 * 跑完把建的裝置、纜線、服務都刪掉，功能開關恢復原狀。
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const TAG = `m2${Date.now().toString(36)}`;

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

const made = { devices: [] as string[], cables: [] as string[], services: [] as string[] };
let swId = "";
let wasEnabled = false;

test.beforeAll(async ({ request }) => {
  const H = { Authorization: `Bearer ${await token(request)}` };
  wasEnabled = (await (await request.get("/api/v1/change-impact/settings", { headers: H })).json()).enabled;
  await request.put("/api/v1/change-impact/settings", { headers: H, data: { enabled: true } });
  const dev = async (name: string, type: string) => {
    const r = await request.post("/api/v1/devices", { headers: H, data: { name: `${TAG}-${name}`, type } });
    expect(r.ok(), await r.text()).toBeTruthy();
    const id = (await r.json()).id as string;
    made.devices.push(id);
    return id;
  };
  const port = async (device_id: string, name: string) => {
    const r = await request.post("/api/v1/device-ports", { headers: H, data: { device_id, name, type: "network" } });
    expect(r.ok(), await r.text()).toBeTruthy();
    return (await r.json()).id as string;
  };
  const cable = async (a: string, b: string) => {
    const c = await request.post("/api/v1/cables", { headers: H, data: { label: `${TAG}-c`, status: "connected" } });
    expect(c.ok(), await c.text()).toBeTruthy();
    const cid = (await c.json()).id as string;
    made.cables.push(cid);
    for (const [side, pid] of [["A", a], ["B", b]] as const) {
      const r = await request.post("/api/v1/cable-terminations",
        { headers: H, data: { cable_id: cid, side, object_type: "device_port", object_id: pid } });
      expect(r.ok(), await r.text()).toBeTruthy();
    }
  };
  swId = await dev("sw", "switch");
  const sw2 = await dev("sw2", "switch");
  const rt = await dev("rt", "router");
  const app = await dev("app", "server");
  const db = await dev("db", "server");
  await cable(await port(app, "eth0"), await port(swId, "ge1"));
  await cable(await port(db, "eth0"), await port(swId, "ge2"));
  await cable(await port(db, "eth1"), await port(sw2, "ge1"));
  await cable(await port(sw2, "ge48"), await port(rt, "p1"));
  const svc = await request.post("/api/v1/change-impact/services", { headers: H, data: {
    name: `${TAG} ERP`, criticality: "critical",
    groups: [{ name: "app", required_count: 1, confirmed: true,
               members: [{ object_type: "device", object_id: app, relation_type: "requires_network" }] }] } });
  expect(svc.ok(), await svc.text()).toBeTruthy();
  made.services.push((await svc.json()).id);
});

test.afterAll(async ({ request }) => {
  const H = { Authorization: `Bearer ${await token(request)}` };
  for (const id of made.services) await request.delete(`/api/v1/change-impact/services/${id}`, { headers: H });
  for (const id of made.cables) await request.delete(`/api/v1/cables/${id}`, { headers: H });
  for (const id of made.devices) await request.delete(`/api/v1/devices/${id}`, { headers: H });
  await request.put("/api/v1/change-impact/settings", { headers: H, data: { enabled: wasEnabled } });
});

test("服務頁：列出服務、打開看得到依賴群組與成員名稱", async ({ page }) => {
  await login(page);
  await page.goto("/change-impact");
  await page.getByTestId("cip-services").click();
  await expect(page).toHaveURL(/\/change-impact\/services/);
  await page.getByTestId("svc-list").getByText(`${TAG} ERP`).click();
  const ed = page.getByTestId("svc-editor");
  await expect(ed).toBeVisible();
  await expect(ed.getByTestId("svc-group")).toHaveCount(1);
  await expect(ed).toContainText(`${TAG}-app`);
  await expect(ed.locator(".n-card__footer .n-button .n-button__icon").first()).toBeVisible();
});

test("裝置頁「變更評估 → 維護評估」：單一上行模型內中斷、另一條路待驗證、服務中斷", async ({ page }) => {
  await login(page);
  await page.goto(`/devices/${swId}`);
  await page.getByTestId("device-change-impact-btn").click();
  await page.locator(".n-dropdown-option", { hasText: "維護評估" }).click();
  await expect(page.getByTestId("cip-wizard")).toBeVisible();
  await page.getByTestId("cip-wiz-submit").click();
  await expect(page).toHaveURL(/\/change-impact\/[0-9a-f-]{36}/, { timeout: 20_000 });
  const findings = page.getByTestId("cip-findings");
  await expect(findings).toContainText(`${TAG}-app`, { timeout: 30_000 });
  await expect(findings).toContainText("模型內中斷");
  await expect(findings).toContainText("備援待驗證");
  await expect(findings).toContainText(`${TAG} ERP`);
  await page.locator(".n-tabs-tab", { hasText: "證據與資料不足" }).click();
  await expect(page.getByTestId("cip-gaps")).toContainText("VLAN");
});
