/**
 * SNMP 收集畫面的共用規則（收集目標表單、憑證表單、測試結果、各頁的狀態欄都用 utils/snmp）。
 *
 * 守的是：
 * - 新增收集目標的預設收集項目跟著裝置類型（路由器讀 ARP、交換器讀 MAC 表與鄰居）
 * - 憑證更新時，留空的機密**不送**（送空字串＝後端以為要清掉／或拒絕），用不到的協定送 null
 * - 收集目標的內容：只有「指定代理」才帶 agent_id、空白覆寫＝null、間隔換成秒
 * - 證據來源的顯示名稱：arp:snmp_live、iface:snmp 不可以印出原始鍵
 */
import { describe, expect, it } from "vitest";
import zh from "../../i18n/zh-TW.json";
import en from "../../i18n/en-US.json";
import ja from "../../i18n/ja-JP.json";
import {
  blankCredentialForm, blankTargetForm, buildCredentialPayload, buildTargetPayload, credentialFormFrom,
  defaultCollectFlags, defaultCredentialId, fmtUptime, sectionText, targetFormFrom, targetHealth,
} from "../snmp";
import { evidenceVendorLabel, fwSeenLabel } from "../investigateSections";
import { probeInstall, toolInstallCmd } from "../probeInstall";
import type { SnmpCredential, SnmpTarget } from "@/api/snmp";

function lookup(dict: unknown, key: string): string | undefined {
  let o: any = dict;
  for (const p of key.split(".")) {
    if (!o || typeof o !== "object" || !(p in o)) return undefined;
    o = o[p];
  }
  return typeof o === "string" ? o : undefined;
}
const t = (key: string, params?: Record<string, unknown>) => {
  const raw = lookup(zh, key);
  if (raw === undefined) return `<<${key}>>`;
  return raw.replace(/\{(\w+)\}/g, (_m, k) => String(params?.[k] ?? `{${k}}`));
};

function cred(over: Partial<SnmpCredential> = {}): SnmpCredential {
  return {
    id: "c1", name: "core-v3", is_default: false, customer_id: null, customer_name: null, version: "v3",
    username: "ipam", security_level: "authPriv", auth_protocol: "SHA-256", priv_protocol: "AES-128",
    timeout_s: 5, retries: 1, description: null, has_community: false, has_auth_passphrase: true,
    has_priv_passphrase: true, target_count: 0, created_at: "", updated_at: "", ...over,
  };
}

function target(over: Partial<SnmpTarget> = {}): SnmpTarget {
  return {
    id: "t1", device_id: "d1", device_name: "rtr1", customer_id: null, credential_id: "c1",
    credential_name: "core-v3", credential_version: "v3", ip_override: null, mgmt_ip: "192.0.2.1", port: 161,
    collect_arp: true, collect_fdb: false, collect_neighbors: false, collect_interfaces: true,
    intervals: null, effective_intervals: {}, collector: "auto", agent_id: null, effective_agent_id: "a1",
    effective_agent_name: "agent-1", collector_subnet_id: null, collector_subnet_cidr: "192.0.2.0/24",
    collector_error: null, enabled: true, auto_create_ips: false, description: null, last_run_at: null,
    last_ok_at: null, last_error: null, last_error_code: null, last_summary: null, created_at: null,
    updated_at: null, ...over,
  };
}

describe("收集項目預設值跟著裝置類型", () => {
  it("路由器／防火牆：ARP＋介面", () => {
    expect(defaultCollectFlags("router")).toEqual({ arp: true, fdb: false, neighbors: false, interfaces: true });
    expect(defaultCollectFlags("firewall")).toEqual({ arp: true, fdb: false, neighbors: false, interfaces: true });
  });
  it("交換器：MAC 表＋鄰居＋介面（不讀 ARP）", () => {
    expect(defaultCollectFlags("switch")).toEqual({ arp: false, fdb: true, neighbors: true, interfaces: true });
  });
  it("其他或不知道：只讀介面", () => {
    for (const ty of ["server", "ap", "other", "", null, undefined]) {
      expect(defaultCollectFlags(ty)).toEqual({ arp: false, fdb: false, neighbors: false, interfaces: true });
    }
  });
  it("空白表單用裝置類型的預設值、預設不自動建立 IP", () => {
    const f = blankTargetForm("switch");
    expect(f.flags.fdb).toBe(true);
    expect(f.auto_create_ips).toBe(false);
    expect(f.collector).toBe("auto");
    expect(f.port).toBe(161);
  });
});

describe("憑證的送出內容", () => {
  it("編輯時機密留空 → 不送（保留已存的值）", () => {
    const f = credentialFormFrom(cred());
    expect(f.auth_passphrase).toBe("");
    const p = buildCredentialPayload(f);
    expect(p).not.toHaveProperty("auth_passphrase");
    expect(p).not.toHaveProperty("priv_passphrase");
    expect(p).not.toHaveProperty("community");
    expect(p.username).toBe("ipam");
    expect(p.priv_protocol).toBe("AES-128");
  });

  it("有填才送；authNoPriv 不帶加密協定與加密密碼", () => {
    const f = credentialFormFrom(cred());
    f.security_level = "authNoPriv";
    f.auth_passphrase = "new-secret-1";
    f.priv_passphrase = "ignored-secret";
    const p = buildCredentialPayload(f);
    expect(p.auth_passphrase).toBe("new-secret-1");
    expect(p.priv_protocol).toBeNull();
    expect(p).not.toHaveProperty("priv_passphrase");
  });

  it("v2c：只送社群字串，v3 欄位一律 null", () => {
    const f = blankCredentialForm();
    f.name = "  lab  ";
    f.version = "v2c";
    f.community = "public-ro";
    f.username = "leftover";
    const p = buildCredentialPayload(f);
    expect(p).toMatchObject({ name: "lab", version: "v2c", community: "public-ro", username: null,
      security_level: null, auth_protocol: null, priv_protocol: null, description: null, customer_id: null });
    expect(p).not.toHaveProperty("auth_passphrase");
  });

  it("v2c 編輯時社群字串留空 → 不送", () => {
    const f = credentialFormFrom(cred({ version: "v2c", has_community: true, username: null,
      security_level: null, auth_protocol: null, priv_protocol: null }));
    expect(buildCredentialPayload(f)).not.toHaveProperty("community");
  });

  it("預設憑證：有標預設就選它；沒有預設時只有一組才選，好幾組不猜", () => {
    expect(defaultCredentialId([cred({ id: "a" }), cred({ id: "b", is_default: true })])).toBe("b");
    expect(defaultCredentialId([cred({ id: "only" })])).toBe("only");
    expect(defaultCredentialId([cred({ id: "a" }), cred({ id: "b" })])).toBeNull();
    expect(defaultCredentialId([])).toBeNull();
  });
});

describe("收集目標的送出內容", () => {
  it("只有「指定代理」才帶 agent_id；空白覆寫＝null；沒勾 ARP 時不自動建立", () => {
    const f = blankTargetForm("switch");
    f.credential_id = "c1";
    f.agent_id = "a1";
    f.auto_create_ips = true;
    f.ip_override = "   ";
    const p = buildTargetPayload(f);
    expect(p.agent_id).toBeNull();
    expect(p.ip_override).toBeNull();
    expect(p.auto_create_ips).toBe(false);
    expect(p.collect_fdb).toBe(true);
    expect(p.intervals).toBeNull();
    f.collector = "agent";
    f.ip_override = " 198.51.100.1 ";
    expect(buildTargetPayload(f).agent_id).toBe("a1");
    expect(buildTargetPayload(f).ip_override).toBe("198.51.100.1");
  });

  it("間隔用分鐘填、送出換成秒；編輯時從秒換回分鐘", () => {
    const f = blankTargetForm("router");
    f.intervals_min.arp = 10;
    expect(buildTargetPayload(f).intervals).toEqual({ arp: 600 });
    const back = targetFormFrom(target({ intervals: { arp: 600, fdb: 1800 } }));
    expect(back.intervals_min).toEqual({ arp: 10, fdb: 30, neighbors: null, interfaces: null });
  });

  it("編輯時照原本的收集項目，不套類型預設值", () => {
    const f = targetFormFrom(target({ collect_arp: false, collect_fdb: true }));
    expect(f.flags).toEqual({ arp: false, fdb: true, neighbors: false, interfaces: true });
  });
});

describe("狀態與結果的文字", () => {
  it("狀態欄：停用 → 收不了 → 有錯 → 正常 → 還沒收過", () => {
    expect(targetHealth(target({ enabled: false, collector_error: { code: "x" } }))).toBe("disabled");
    expect(targetHealth(target({ collector_error: { code: "snmp_no_collector" } }))).toBe("no_collector");
    expect(targetHealth(target({ last_error_code: "snmp_sections_failed", last_ok_at: "x" }))).toBe("failed");
    expect(targetHealth(target({ last_ok_at: "2026-10-15T00:00:00Z" }))).toBe("ok");
    expect(targetHealth(target())).toBe("never");
  });

  it("區段結果講成一句話（列數、設備沒有這個表、失敗原因、這次沒讀）", () => {
    expect(sectionText({ status: "ok", rows: 42 }, t)).toBe("42 列");
    expect(sectionText({ status: "ok", rows: 9, truncated: true }, t)).toContain("列數上限");
    expect(sectionText({ status: "absent" }, t)).toBe("設備沒有這個表");
    expect(sectionText({ status: "error", error: "Timeout" }, t)).toBe("失敗：Timeout");
    expect(sectionText(undefined, t)).toBe("這次沒有讀");
  });

  it("開機時間（TimeTicks 是百分之一秒）", () => {
    expect(fmtUptime(100 * (2 * 86400 + 3 * 3600 + 4 * 60), t)).toBe("2 天 3 小時 4 分");
    expect(fmtUptime(100 * 3700, t)).toBe("1 小時 1 分");
    expect(fmtUptime(null, t)).toBe("—");
  });
});

describe("SNMP 證據來源與安裝指令", () => {
  it("arp:snmp／arp:snmp_live／iface:snmp 都有人看得懂的名稱", () => {
    expect(fwSeenLabel("arp:snmp", t)).toBe("ARP 表（SNMP）");
    expect(fwSeenLabel("arp:snmp_live", t)).toBe("ARP 表（SNMP，reachable）");
    expect(fwSeenLabel("iface:snmp", t)).toBe("設備介面位址（SNMP）");
    expect(evidenceVendorLabel("snmp", t)).toBe("SNMP");
  });

  it("net-snmp 工具的安裝提示含三種套件管理器", () => {
    const line = probeInstall("snmp", t);
    expect(line).toContain("apt install snmp");
    expect(line).toContain("net-snmp-utils");
    expect(line).toContain("net-snmp-tools");
    expect(toolInstallCmd("snmpbulkwalk", "snmp", t)).toBe(line);
    expect(toolInstallCmd("nmap", "nmap", t)).toBe("sudo apt install nmap");
  });

  it("新增的 SNMP 字串三語都有，而且不含 vue-i18n 的特殊字元", () => {
    const keys = Object.keys((zh as any).snmp);
    expect(keys.length).toBeGreaterThan(100);
    for (const k of keys) {
      for (const [name, dict] of [["en", en], ["ja", ja]] as const) {
        expect(lookup(dict, `snmp.${k}`), `${name}: snmp.${k}`).toBeTypeOf("string");
      }
      for (const dict of [zh, en, ja]) {
        const v = lookup(dict, `snmp.${k}`)!;
        expect(v.replace(/\{[a-z_]+\}/g, ""), `snmp.${k}`).not.toMatch(/[@{}|]/);
      }
    }
  });
});
