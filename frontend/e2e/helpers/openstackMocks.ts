import { type Page, type Route } from "@playwright/test";

/** OpenStack 整合的 API 模擬（openstack.spec.ts 與畫面截圖共用）：不需要真的 OpenStack。 */
export const NOW = new Date().toISOString();
export function inst(over: Record<string, unknown> = {}) {
  return {
    id: "11111111-1111-4111-8111-111111111111", name: "lab-cube", product: "cubecos",
    auth_url: "https://10.9.8.7:5000/v3", auth_method: "password", username: "ipam", user_domain: "Default",
    project_name: "admin", project_domain: "Default", app_credential_id: null, has_secret: true, region: null,
    interface: "internal", verify_tls: true, has_ca_cert: true, all_projects: true, cubecos_url: null,
    enabled: true, cluster_id: null, sync_interval_seconds: 300, last_sync_at: NOW, last_error: null,
    last_full_success_at: NOW, scope_subnet_ids: null, auto_create_ips: false, description: "實驗室",
    capabilities: {
      services: { identity: { ok: true, url: "https://10.9.8.7:5000/v3" }, compute: { ok: true, url: "https://10.9.8.7:8774/v2.1" },
                  network: { ok: true, url: "https://10.9.8.7:9696" }, cubecos: { ok: true, url: "https://10.9.8.7" } },
      all_projects: true, host_visible: true, port_binding_visible: true, nova_microversion: "2.79",
      projects_visible: 4, checked_at: NOW,
    },
    vm_count: 12, port_count: 30, node_count: 3,
    ...over,
  };
}
export const INST2 = inst({ id: "22222222-2222-4222-8222-222222222222", name: "public-os", product: "openstack",
  auth_method: "app_credential", username: null, project_name: null, app_credential_id: "appcred-1",
  has_ca_cert: false, capabilities: null, vm_count: 0, port_count: 0, node_count: 0, last_sync_at: null,
  last_full_success_at: null, last_error: "連不上 OpenStack：timed out" });

export interface Mock { list: any[]; posted: any[]; patched: any[]; mapped: any[]; taskPolls: number }

export async function mockOpenStack(page: Page, m: Mock) {
  // 選單「已設定」與虛擬化選單項目：讓 openstack 顯示為已設定
  await page.route("**/integration-presence", async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    await route.fulfill({ response: resp, json: { ...body, openstack: true } });
  });
  await page.route(/\/api\/v1\/openstack-instances(\/.*)?(\?.*)?$/, async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname.replace(/^.*\/api\/v1\/openstack-instances/, "");
    const method = req.method();
    if (p === "" && method === "GET") return route.fulfill({ json: m.list });
    if (p === "" && method === "POST") {
      const body = req.postDataJSON();
      m.posted.push(body);
      const created = inst({ ...body, id: "33333333-3333-4333-8333-333333333333", has_secret: true, has_ca_cert: false,
        capabilities: null, vm_count: 0, port_count: 0, node_count: 0 });
      delete (created as any).secret;
      m.list = [...m.list, created];
      return route.fulfill({ status: 201, json: created });
    }
    const one = /^\/([0-9a-f-]{36})$/.exec(p);
    if (one && method === "PATCH") {
      const body = req.postDataJSON();
      m.patched.push(body);
      m.list = m.list.map((x) => (x.id === one[1] ? { ...x, ...body, has_ca_cert: body.ca_cert === "" ? false : x.has_ca_cert } : x));
      return route.fulfill({ json: m.list.find((x) => x.id === one[1]) });
    }
    if (one && method === "DELETE") {
      m.list = m.list.filter((x) => x.id !== one[1]);
      return route.fulfill({ status: 204, body: "" });
    }
    if (/\/test$/.test(p)) {
      return route.fulfill({ json: {
        ok: false,
        capabilities: { services: { identity: { ok: true, url: "https://10.9.8.7:5000/v3" },
                                    compute: { ok: true, url: "https://10.9.8.7:8774/v2.1" },
                                    network: { ok: false, code: "openstack_no_catalog_service", params: { service: "network" } },
                                    cubecos: { ok: false, code: "cubecos_unreachable", message: "connection refused" } },
                        all_projects: false, host_visible: false, port_binding_visible: true,
                        nova_microversion: "2.79", projects_visible: 1, checked_at: NOW },
        error: { code: "openstack_no_catalog_service", params: { service: "network" }, message: "no network" },
      } });
    }
    if (/\/sync$/.test(p)) return route.fulfill({ json: { task_id: "44444444-4444-4444-8444-444444444444" } });
    if (/\/subnets$/.test(p)) {
      return route.fulfill({ json: [
        { id: "s-auto", subnet_id: "aaaa-1", name: "mgmt-sub", network_name: "mgmt", project_name: "admin",
          cidr: "10.20.0.0/24", gateway_ip: "10.20.0.1", enable_dhcp: true, shared: true,
          mapped_subnet_id: "840acfe0-daf6-4d0e-8d0f-69a99caa235f", mapped_manually: false, mapped_subnet_cidr: "10.20.0.0/24" },
        { id: "s-pend", subnet_id: "bbbb-2", name: "tenant-sub", network_name: "tenant-net", project_name: "demo",
          cidr: "192.168.77.0/24", gateway_ip: "192.168.77.1", enable_dhcp: false, shared: false,
          mapped_subnet_id: null, mapped_manually: false, mapped_subnet_cidr: null },
      ] });
    }
    const map = /\/subnets\/([^/]+)$/.exec(p);
    if (map && method === "PATCH") {
      const body = req.postDataJSON();
      m.mapped.push({ row: map[1], ...body });
      return route.fulfill({ json: { id: map[1], mapped_subnet_id: body.mapped_subnet_id, mapped_manually: true } });
    }
    if (/\/nodes$/.test(p)) {
      return route.fulfill({ json: [
        { hostname: "cube-01", role: "control", status: "ok", management_ip: "10.9.8.11", provider_ip: null, storage_ip: "10.99.0.11", serial: "SN001" },
        { hostname: "cube-02", role: "compute", status: "down", management_ip: "10.9.8.12", provider_ip: null, storage_ip: "10.99.0.12", serial: null },
      ] });
    }
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  // 背景作業：前兩次輪詢還在跑（按鈕要反灰＋轉圈），之後完成
  await page.route("**/api/v1/tasks/44444444-4444-4444-8444-444444444444", async (route) => {
    m.taskPolls += 1;
    const done = m.taskPolls > 2;
    await route.fulfill({ json: {
      id: "44444444-4444-4444-8444-444444444444", kind: "openstack.sync", status: done ? "succeeded" : "running",
      trigger: "manual", target_type: "openstack_instance", target_id: m.list[0]?.id, target_label: "lab-cube",
      actor_user_id: null, progress: done ? 100 : 40, summary: done ? { vms: 12 } : null, error: null,
      queued_at: NOW, started_at: NOW, finished_at: done ? NOW : null,
    } });
  });
}

export function newMock(): Mock {
  return { list: [inst(), INST2], posted: [], patched: [], mapped: [], taskPolls: 0 };
}

/** 虛擬化頁：一個 OpenStack 叢集、一台 VM（專案寫在說明） */
export async function mockOpenStackVms(page: Page) {
  const CL = "55555555-5555-4555-8555-555555555555";
  await page.route(/\/api\/v1\/virt\/clusters(\?.*)?$/, (route) => route.fulfill({ json: {
    items: [{ id: CL, name: "lab-cube", type: "openstack", description: null }], total: 1, page: 1, page_size: 500 } }));
  await page.route(/\/api\/v1\/virt\/vms(\?.*)?$/, (route) => route.fulfill({ json: {
    items: [{ id: "66666666-6666-4666-8666-666666666666", cluster_id: CL, legacy_vmid: null, name: "web-a",
              node: "compute-03", kind: "vm", status: "running", vcpus: 2, memory_mb: 4096, disk_gb: 40,
              primary_ip_id: null, is_template: false, os_name: null, description: "project: demo",
              ips: ["192.168.77.10"], macs: ["fa:16:3e:00:00:01"], bridges: ["tenant-net"], ip_links: {}, ips_by_mac: [] }],
    total: 1, page: 1, page_size: 500 } }));
}
