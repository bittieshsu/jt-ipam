/**
 * 把 AI 工具名稱（如 `get_ip_detail`）轉成看得懂的說明。
 *
 * 刻意用「動詞＋名詞」組合而不是逐一列表：工具有 85 個而且會再增加，
 * 硬表一定會過時，屆時畫面就會顯示原始英文代號 —— 那對使用者沒有意義。
 */
const VERBS: Record<string, string> = {
  get: "查詢", list: "列出", search: "搜尋", find: "尋找", check: "檢查",
  count: "統計", suggest: "建議", next: "尋找可用", resolve: "解析", trace: "追蹤",
  create: "建立", update: "更新", delete: "刪除", assign: "配發", reserve: "保留",
};

const NOUNS: Record<string, string> = {
  ip: "IP", ips: "IP", subnet: "子網路", subnets: "子網路", section: "區段",
  device: "裝置", devices: "裝置", vlan: "VLAN", vrf: "VRF", dns: "DNS",
  arp: "ARP", fdb: "MAC 位址表", mac: "MAC", vm: "虛擬機", vms: "虛擬機",
  rack: "機櫃", racks: "機櫃", location: "機房", locations: "機房",
  customer: "單位", customers: "單位", nat: "NAT", firewall: "防火牆",
  anomalies: "異常", anomaly: "異常", topology: "拓樸", detail: "詳細資料",
  history: "歷程", usage: "使用率", free: "可用位址", available: "可用位址",
  cert: "憑證", certificates: "憑證", power: "電力", cable: "佈線",
  wazuh: "Wazuh", zabbix: "Zabbix", librenms: "LibreNMS", proxmox: "Proxmox",
  dhcp: "DHCP", lease: "租約", leases: "租約", port: "連接埠", ports: "連接埠",
  surface: "對外曝險", attack: "對外曝險", agents: "代理", requests: "申請",
  changes: "異動", audit: "稽核", stats: "統計", summary: "摘要",
};

export function humanToolName(raw: string | undefined | null): string {
  if (!raw) return "";
  const parts = raw.toLowerCase().split(/[_\s]+/).filter(Boolean);
  if (!parts.length) return raw;
  const verb = VERBS[parts[0]];
  const rest = (verb ? parts.slice(1) : parts)
    .map((w) => NOUNS[w] || w)
    .filter(Boolean)
    .join("");
  if (!verb) return rest || raw;
  return rest ? `${verb}${rest}` : verb;
}
