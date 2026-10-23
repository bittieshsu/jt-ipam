import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post, patch, del } = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn(),
}));
vi.mock("../client", () => ({ apiClient: { get, post, patch, delete: del } }));

import {
  OpenStack, blankOpenStackForm, buildOpenStackPayload, capabilityItems, defaultCubecosUrl,
  formFromInstance, mappingState, serviceRows, type OpenStackInstance,
} from "../openstack";

const inst: OpenStackInstance = {
  id: "i1", name: "cube", product: "cubecos", auth_url: "https://10.0.0.5:5000/v3", auth_method: "password",
  username: "ipam", user_domain: "Default", project_name: "admin", project_domain: "Default",
  app_credential_id: null, has_secret: true, region: null, interface: "public", verify_tls: true,
  has_ca_cert: true, all_projects: true, cubecos_url: null, enabled: true, sync_interval_seconds: 300,
  scope_subnet_ids: null, auto_create_ips: false, description: "lab",
};

describe("OpenStack API 路徑", () => {
  beforeEach(() => { [get, post, patch, del].forEach((f) => f.mockReset().mockResolvedValue({ data: {} })); });

  it("每支端點都打在 /api/v1/openstack-instances 底下", async () => {
    await OpenStack.list();
    await OpenStack.test("i1");
    await OpenStack.sync("i1");
    await OpenStack.subnets("i1");
    await OpenStack.mapSubnet("i1", "r1", "s9");
    await OpenStack.nodes("i1");
    await OpenStack.portsByIp("a1");
    await OpenStack.remove("i1");
    expect(get.mock.calls.map((c) => c[0])).toEqual([
      "/api/v1/openstack-instances", "/api/v1/openstack-instances/i1/subnets",
      "/api/v1/openstack-instances/i1/nodes", "/api/v1/openstack-instances/ports/by-ip/a1",
    ]);
    expect(post.mock.calls.map((c) => c[0])).toEqual([
      "/api/v1/openstack-instances/i1/test", "/api/v1/openstack-instances/i1/sync",
    ]);
    expect(patch).toHaveBeenCalledWith("/api/v1/openstack-instances/i1/subnets/r1", { mapped_subnet_id: "s9" });
    expect(del).toHaveBeenCalledWith("/api/v1/openstack-instances/i1");
  });

  it("取消對應送 null（不是省略）", async () => {
    await OpenStack.mapSubnet("i1", "r1", null);
    expect(patch).toHaveBeenCalledWith("/api/v1/openstack-instances/i1/subnets/r1", { mapped_subnet_id: null });
  });
});

describe("buildOpenStackPayload", () => {
  it("新增：密碼一定送、沒用到的應用程式憑證欄位送 null", () => {
    const f = { ...blankOpenStackForm(), name: " os1 ", auth_url: "https://ks:5000", username: "ipam",
      project_name: "admin", secret: "pw" };
    const p = buildOpenStackPayload(f, false);
    expect(p).toMatchObject({ name: "os1", auth_method: "password", username: "ipam", project_name: "admin",
      app_credential_id: null, secret: "pw", cubecos_url: null, region: null, scope_subnet_ids: null });
    expect(p).not.toHaveProperty("ca_cert");
  });

  it("編輯：密碼留空＝不送（不會把既有密碼洗掉）", () => {
    const p = buildOpenStackPayload(formFromInstance(inst), true);
    expect(p).not.toHaveProperty("secret");
    expect(p).not.toHaveProperty("ca_cert");
  });

  it("應用程式憑證：帳號與專案送 null、憑證 ID 照填", () => {
    const f = { ...formFromInstance(inst), auth_method: "app_credential" as const, app_credential_id: " abc ",
      secret: "s3cret" };
    const p = buildOpenStackPayload(f, true);
    expect(p).toMatchObject({ username: null, project_name: null, app_credential_id: "abc", secret: "s3cret" });
  });

  it("CA 憑證：貼新的就送、按清除送空字串、都沒有就不送", () => {
    const base = formFromInstance(inst);
    expect(buildOpenStackPayload({ ...base, ca_cert: "-----BEGIN CERTIFICATE-----\nx\n" }, true).ca_cert)
      .toBe("-----BEGIN CERTIFICATE-----\nx");
    expect(buildOpenStackPayload({ ...base, ca_cert_clear: true }, true).ca_cert).toBe("");
    expect(buildOpenStackPayload(base, true)).not.toHaveProperty("ca_cert");
  });

  it("CubeCOS 網址只有產品是 CubeCOS 才送", () => {
    const f = { ...formFromInstance(inst), cubecos_url: "https://cube.lab" };
    expect(buildOpenStackPayload(f, true).cubecos_url).toBe("https://cube.lab");
    expect(buildOpenStackPayload({ ...f, product: "openstack" }, true).cubecos_url).toBeNull();
  });

  it("說明清空時送空字串（不是 undefined，後端才會真的清掉）", () => {
    const p = buildOpenStackPayload({ ...formFromInstance(inst), description: "" }, true);
    expect(p.description).toBe("");
    expect(JSON.parse(JSON.stringify(p))).toHaveProperty("description", "");
  });

  it("網域留空退回 Default、範圍有選才送清單", () => {
    const f = { ...blankOpenStackForm(), user_domain: " ", project_domain: "", scope_subnet_ids: ["s1"] };
    const p = buildOpenStackPayload(f, false);
    expect(p.user_domain).toBe("Default");
    expect(p.project_domain).toBe("Default");
    expect(p.scope_subnet_ids).toEqual(["s1"]);
  });
});

describe("defaultCubecosUrl", () => {
  it("取 Keystone 網址的主機、去掉埠與路徑", () => {
    expect(defaultCubecosUrl("https://10.0.0.5:5000/v3")).toBe("https://10.0.0.5");
    expect(defaultCubecosUrl("keystone.lab:5000")).toBe("https://keystone.lab");
    expect(defaultCubecosUrl("http://[2001:db8::1]:5000/v3")).toBe("https://[2001:db8::1]");
  });
  it("還沒填或填錯時給範例", () => {
    expect(defaultCubecosUrl("")).toBe("https://cube.example");
    expect(defaultCubecosUrl("http://")).toBe("https://cube.example");
  });
});

describe("capabilityItems／serviceRows", () => {
  it("沒有資料的項目是 null（不是失敗）", () => {
    expect(capabilityItems(null).every((x) => x.ok === null)).toBe(true);
  });
  it("布林照實、微版本與專案數帶值", () => {
    const items = capabilityItems({ all_projects: false, host_visible: true, port_binding_visible: false,
      nova_microversion: "2.79", projects_visible: 0 });
    const by = Object.fromEntries(items.map((x) => [x.key, x]));
    expect(by.all_projects.ok).toBe(false);
    expect(by.host_visible.ok).toBe(true);
    expect(by.port_binding_visible.ok).toBe(false);
    expect(by.nova_microversion).toMatchObject({ ok: true, value: "2.79" });
    expect(by.projects_visible).toMatchObject({ ok: false, value: "0" });
  });
  it("CubeCOS 服務只在產品是 CubeCOS 或結果裡有它時列出", () => {
    expect(serviceRows(null, "openstack").map((r) => r.key)).toEqual(["identity", "compute", "network"]);
    expect(serviceRows(null, "cubecos").map((r) => r.key)).toEqual(["identity", "compute", "network", "cubecos"]);
    const rows = serviceRows({ services: { identity: { ok: true, url: "https://ks/v3" } } }, "openstack");
    expect(rows[0].state).toMatchObject({ ok: true });
    expect(rows[1].state).toBeNull();
  });
});

describe("mappingState", () => {
  it("四種狀態", () => {
    expect(mappingState({ mapped_subnet_id: null, mapped_manually: false })).toBe("pending");
    expect(mappingState({ mapped_subnet_id: null, mapped_manually: true })).toBe("none");
    expect(mappingState({ mapped_subnet_id: "s", mapped_manually: false })).toBe("auto");
    expect(mappingState({ mapped_subnet_id: "s", mapped_manually: true })).toBe("manual");
  });
});
