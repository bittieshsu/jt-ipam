/**
 * 主機名稱來源要看得出「哪一台回報的、最後一次是什麼時候、跟目前的名稱一不一樣」（客戶 2026-10-11）。
 *
 * 客戶在比對各台掃描代理抓到的名字：Windows 的 NetBIOS 名稱和主機名稱可以不一樣（被坑過），
 * 以前面板只寫「NetBIOS（掃描代理）」看不出是哪一台；時間是名稱最後改變的時間，殘留的舊名字看起來跟剛回報的一樣。
 * 樣本（tests/seed_e2e.py）：10.20.0.78 —— DNS（e2e-ns1）e2e-fileserver-prod.corp.example、
 * NetBIOS（e2e-name-agent）E2E-FILESERVER（和 DNS 不同）、mDNS（e2e-name-agent）三天前回報。
 */
import { test, expect } from "@playwright/test";

const ADMIN_USER = process.env.E2E_ADMIN_USER || "admin";
const ADMIN_PASS = process.env.E2E_ADMIN_PASS || "";
const API = process.env.E2E_API_URL || "http://127.0.0.1:8010";
test.skip(!ADMIN_PASS, "需要 E2E_ADMIN_PASS");

async function ok(res: any, what: string) {
  if (!res.ok()) throw new Error(`${what} 失敗 HTTP ${res.status()}：${await res.text()}`);
  return res;
}

test("來源標籤：回報者、幾天前、名稱不同", async ({ page, request }) => {
  const login = await ok(await request.post(`${API}/api/v1/auth/login`, {
    data: { username: ADMIN_USER, password: ADMIN_PASS } }), "登入");
  const h = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const found = await (await ok(await request.get(`${API}/api/v1/addresses?q=10.20.0.78`, { headers: h }), "查位址")).json();
  const row = (found.items ?? []).find((a: any) => a.ip === "10.20.0.78");
  if (!row) throw new Error("找不到 seed_e2e 的 10.20.0.78（先跑 seed_e2e）");

  await page.goto("/login");
  await page.getByPlaceholder(/帳號|Username/).fill(ADMIN_USER);
  await page.getByPlaceholder(/密碼|Password/).fill(ADMIN_PASS);
  await page.getByRole("button", { name: /登入/ }).click();
  await page.waitForURL((u: URL) => !u.pathname.includes("/login"));
  await page.goto(`/addresses/${row.id}`);

  const nb = page.getByTestId("hostname-src-netbios");
  await expect(nb).toContainText("E2E-FILESERVER", { timeout: 20_000 });
  await nb.hover();
  const nbTip = page.getByTestId("hostname-src-tip-netbios");
  await expect(nbTip).toContainText("e2e-name-agent");
  await expect(nbTip).toContainText("和目前採用的名稱「e2e-fileserver-prod.corp.example」不同");
  await expect(nbTip).toContainText(/最後看到 \d{4}/);

  // 三天沒人回報：標籤上直接看得到
  await expect(page.getByTestId("hostname-src-age-mdns")).toContainText("3 天前");
  await expect(page.getByTestId("hostname-src-age-netbios")).toHaveCount(0);
  // mDNS 的名字和 DNS 只差在 FQDN：不算不同
  await page.getByTestId("hostname-src-mdns").hover();
  await expect(page.getByTestId("hostname-src-tip-mdns")).not.toContainText("不同");

  await page.getByTestId("hostname-src-dns").hover();
  await expect(page.getByTestId("hostname-src-tip-dns")).toContainText("e2e-ns1");
});
