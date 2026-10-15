/**
 * SNMP 收集畫面共用的純函式（收集目標表單、憑證表單、測試結果、各頁的狀態欄都用這一份）。
 *
 * 放在這裡而不是各元件裡：同一個物件有好幾個入口（SNMP 頁、裝置詳情、子網路的閘道按鈕），
 * 預設值與送出的內容一旦各寫一份就會不一樣（memory: feedback_same_object_same_edit_ui）。
 */
import { srvText } from "@/utils/wsError";
import {
  SNMP_SECTIONS, type SnmpCollector, type SnmpCredential, type SnmpCredentialWrite,
  type SnmpSection, type SnmpSectionResult, type SnmpSecurityLevel, type SnmpTarget,
  type SnmpTargetWrite, type SnmpVersion,
} from "@/api/snmp";

type T = (key: string, params?: Record<string, unknown>) => string;

export type CollectFlags = Record<SnmpSection, boolean>;

/** 收集目標表單的預設對象：從裝置頁進來（固定那台） */
export interface SnmpPresetDevice { id: string; name: string; type?: string | null }
/** 從子網路的閘道進來：給一筆 IP（沒連結裝置時後端以它建一台 router） */
export interface SnmpPresetIp {
  id: string; ip: string; deviceId?: string | null; deviceName?: string | null; deviceType?: string | null;
}

/**
 * 新增收集目標時，收集項目的預設值跟著裝置類型走：
 * - 路由器／防火牆：ARP（補隔著它的網段的 MAC）＋介面
 * - 交換器：MAC 表＋鄰居（LLDP/CDP）＋介面
 * - 其他：只讀介面（不知道它是什麼，先讀最輕的那一段）
 */
export function defaultCollectFlags(deviceType: string | null | undefined): CollectFlags {
  const type = String(deviceType ?? "").toLowerCase();
  if (type === "router" || type === "firewall") {
    return { arp: true, fdb: false, neighbors: false, interfaces: true };
  }
  if (type === "switch") {
    return { arp: false, fdb: true, neighbors: true, interfaces: true };
  }
  return { arp: false, fdb: false, neighbors: false, interfaces: true };
}

export interface CredentialForm {
  name: string;
  is_default: boolean;
  customer_id: string | null;
  version: SnmpVersion;
  community: string;
  username: string;
  security_level: SnmpSecurityLevel;
  auth_protocol: string;
  auth_passphrase: string;
  priv_protocol: string;
  priv_passphrase: string;
  timeout_s: number;
  retries: number;
  description: string;
}

export function blankCredentialForm(): CredentialForm {
  return {
    name: "", is_default: false, customer_id: null, version: "v3",
    community: "", username: "", security_level: "authPriv",
    auth_protocol: "SHA-256", auth_passphrase: "", priv_protocol: "AES-128", priv_passphrase: "",
    timeout_s: 5, retries: 1, description: "",
  };
}

export function credentialFormFrom(c: SnmpCredential): CredentialForm {
  return {
    name: c.name, is_default: c.is_default, customer_id: c.customer_id, version: c.version,
    community: "", username: c.username ?? "",
    security_level: c.security_level ?? "authPriv",
    auth_protocol: c.auth_protocol ?? "SHA-256", auth_passphrase: "",
    priv_protocol: c.priv_protocol ?? "AES-128", priv_passphrase: "",
    timeout_s: c.timeout_s, retries: c.retries, description: c.description ?? "",
  };
}

/**
 * 憑證表單 → API 內容。機密欄位留空就**不送**：更新時留空＝保留已存的值；建立時沒填就讓後端回
 * 「要填社群字串／密碼」的錯誤（比在這裡擋更能講清楚是哪一個欄位）。
 * 用不到的欄位一律送 null（v2c 不帶 v3 的帳號與協定、authNoPriv 不帶加密協定），
 * 不會留下讓人誤會的舊設定。
 */
export function buildCredentialPayload(f: CredentialForm): SnmpCredentialWrite {
  const out: SnmpCredentialWrite = {
    name: f.name.trim(),
    is_default: f.is_default,
    customer_id: f.customer_id || null,
    version: f.version,
    timeout_s: f.timeout_s,
    retries: f.retries,
    description: f.description.trim() || null,
  };
  if (f.version === "v2c") {
    out.username = null;
    out.security_level = null;
    out.auth_protocol = null;
    out.priv_protocol = null;
    if (f.community) out.community = f.community;
    return out;
  }
  out.username = f.username.trim() || null;
  out.security_level = f.security_level;
  out.auth_protocol = f.auth_protocol;
  if (f.auth_passphrase) out.auth_passphrase = f.auth_passphrase;
  if (f.security_level === "authPriv") {
    out.priv_protocol = f.priv_protocol;
    if (f.priv_passphrase) out.priv_passphrase = f.priv_passphrase;
  } else {
    out.priv_protocol = null;
  }
  return out;
}

export interface TargetForm {
  device_id: string | null;
  credential_id: string | null;
  port: number;
  flags: CollectFlags;
  collector: SnmpCollector;
  agent_id: string | null;
  enabled: boolean;
  auto_create_ips: boolean;
  ip_override: string;
  /** 各區段間隔（分鐘）；null＝用預設 */
  intervals_min: Record<SnmpSection, number | null>;
  description: string;
}

export function blankTargetForm(deviceType?: string | null): TargetForm {
  return {
    device_id: null, credential_id: null, port: 161, flags: defaultCollectFlags(deviceType),
    collector: "auto", agent_id: null, enabled: true, auto_create_ips: false, ip_override: "",
    intervals_min: { arp: null, fdb: null, neighbors: null, interfaces: null }, description: "",
  };
}

export function targetFormFrom(tg: SnmpTarget): TargetForm {
  const iv = (s: SnmpSection) => {
    const v = tg.intervals?.[s];
    return typeof v === "number" ? Math.round(v / 60) : null;
  };
  return {
    device_id: tg.device_id, credential_id: tg.credential_id, port: tg.port,
    flags: { arp: tg.collect_arp, fdb: tg.collect_fdb, neighbors: tg.collect_neighbors,
             interfaces: tg.collect_interfaces },
    collector: tg.collector, agent_id: tg.agent_id, enabled: tg.enabled, auto_create_ips: tg.auto_create_ips,
    ip_override: tg.ip_override ?? "",
    intervals_min: { arp: iv("arp"), fdb: iv("fdb"), neighbors: iv("neighbors"), interfaces: iv("interfaces") },
    description: tg.description ?? "",
  };
}

/** 收集目標表單 → API 內容（建立與更新共用；建立時另外帶 device_id 或 ip_address_id） */
export function buildTargetPayload(f: TargetForm): SnmpTargetWrite {
  const intervals: Partial<Record<SnmpSection, number>> = {};
  for (const s of SNMP_SECTIONS) {
    const m = f.intervals_min[s];
    if (typeof m === "number" && m > 0) intervals[s] = Math.round(m * 60);
  }
  return {
    credential_id: f.credential_id || null,
    port: f.port,
    collect_arp: f.flags.arp,
    collect_fdb: f.flags.fdb,
    collect_neighbors: f.flags.neighbors,
    collect_interfaces: f.flags.interfaces,
    collector: f.collector,
    agent_id: f.collector === "agent" ? (f.agent_id || null) : null,
    enabled: f.enabled,
    auto_create_ips: f.flags.arp ? f.auto_create_ips : false,
    ip_override: f.ip_override.trim() || null,
    intervals: Object.keys(intervals).length ? intervals : null,
    description: f.description.trim() || null,
  };
}

/** 新增時預設選「預設憑證」；沒有預設就選唯一的一組（不猜好幾組裡的哪一組） */
export function defaultCredentialId(creds: SnmpCredential[]): string | null {
  const d = creds.find((c) => c.is_default);
  if (d) return d.id;
  return creds.length === 1 ? creds[0].id : null;
}

/** sysUpTime（百分之一秒）→「12 天 3 小時 4 分」 */
export function fmtUptime(ticks: number | null | undefined, t: T): string {
  if (ticks == null || !Number.isFinite(ticks) || ticks < 0) return "—";
  const sec = Math.floor(ticks / 100);
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return t("snmp.uptime_dhm", { d, h, m });
  if (h > 0) return t("snmp.uptime_hm", { h, m });
  return t("snmp.uptime_m", { m });
}

export function sectionLabel(s: SnmpSection | string, t: T): string {
  return t(`snmp.section_${s}`);
}

/** 區段狀態 → 標籤顏色 */
export function sectionTagType(st: string | undefined): "success" | "warning" | "error" | "default" {
  if (st === "ok") return "success";
  if (st === "partial") return "warning";
  if (st === "error") return "error";
  return "default";
}

/**
 * 一個區段的結果講成一句話：讀到幾列、設備沒有這個表、或為什麼失敗。
 * `undefined`＝這一次沒有讀這個區段（沒勾、或沒到期）。
 */
export function sectionText(r: SnmpSectionResult | undefined, t: T): string {
  if (!r) return t("snmp.sec_not_run");
  const rows = typeof r.rows === "number" ? r.rows : 0;
  const trunc = r.truncated ? t("snmp.sec_truncated") : "";
  switch (r.status) {
    case "ok": return t("snmp.sec_rows", { n: rows }) + trunc;
    case "absent": return t("snmp.sec_absent");
    case "partial": return t("snmp.sec_partial", { n: rows }) + trunc + (r.error ? `：${r.error}` : "");
    default: return r.error ? t("snmp.sec_error", { reason: r.error }) : t("snmp.sec_error_unknown");
  }
}

/**
 * 錯誤代碼＋底層原文 → 一行字：有翻譯就用翻譯，原文不同時接在後面（原因不可以被翻譯蓋掉，
 * memory: project_transport_error_detail）。
 */
export function errText(code: string | null | undefined, raw: string | null | undefined): string {
  const tr = code ? srvText({ code, message: raw ?? "" }) : "";
  if (!tr) return raw ?? "";
  if (raw && raw !== tr && !tr.includes(raw)) return `${tr}（${raw}）`;
  return tr;
}

export type TargetHealth = "disabled" | "no_collector" | "failed" | "ok" | "never";

/** 收集目標的狀態欄：停用 → 收不了（收集者判定不出來）→ 上次有錯 → 正常 → 還沒收過 */
export function targetHealth(tg: SnmpTarget): TargetHealth {
  if (!tg.enabled) return "disabled";
  if (tg.collector_error) return "no_collector";
  if (tg.last_error_code || tg.last_error) return "failed";
  if (tg.last_ok_at) return "ok";
  return "never";
}

export const HEALTH_TAG: Record<TargetHealth, "default" | "error" | "warning" | "success" | "info"> = {
  disabled: "default", no_collector: "error", failed: "warning", ok: "success", never: "info",
};

/** 收集記錄的結果 → 標籤顏色 */
export function runResultType(r: string): "success" | "warning" | "error" | "info" | "default" {
  return ({ success: "success", partial: "warning", failed: "error", running: "info" } as
    Record<string, "success" | "warning" | "error" | "info">)[r] ?? "default";
}
