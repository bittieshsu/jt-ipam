<script setup lang="ts">
/**
 * 通用 IP 編輯 modal。
 * SubnetDetail / Addresses 都用同一個。
 *
 * 預設 read-only 顯示完整欄位；按「編輯」進 edit 模式才能改。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NModal, NCard, NSpace, NButton, NDescriptions, NDescriptionsItem,
  NForm, NFormItem, NInput, NSelect, NSwitch, NPopconfirm, NTag, NIcon, NPagination,
  NCollapse, NCollapseItem, NTimeline, NTimelineItem, NText, NEmpty, NSpin,
  NTooltip, NCheckbox, NCheckboxGroup, NButtonGroup, NDivider,
  NInputGroup, NInputGroupLabel,
  useMessage,
} from "naive-ui";
import { useAuthStore } from "@/stores/auth";
import { apiClient } from "@/api/client";
import type { IPAddress } from "@/types";
import {
  updateAddress, deleteAddress, createAddress, getDeviceSuggestion, applyDeviceSuggestion,
  type IPAddressUpdate, type DeviceSuggestion,
} from "@/api/addresses";
import { getAddressHistory, getAddressSwitchPort, type HistoryFacet, type IPChangeLog,
  type SwitchPortInfo } from "@/api/ip_history";
import { getHostnameSources, type HostnameSources } from "@/api/hostname";
import { EditIcon, SaveIcon, CancelIcon, DeleteIcon, PlusIcon, LinkIcon, TerminalIcon, DisplayIcon, VncIcon, NoVncIcon, SearchIcon , FilesIcon } from "@/icons";
import InvestigateModal from "@/components/InvestigateModal.vue";
import ChangeValue from "@/components/ChangeValue.vue";
import IpRoleTags from "@/components/IpRoleTags.vue";
import { ArrowLeft as ArrowLeftIcon } from "@iconoir/vue";
import { fmtDateTime } from "@/utils/datetime";
import { useCustomers } from "@/composables/useCustomers";
import { useChangeLogDim } from "@/composables/useChangeLogDim";
import { useRouter } from "vue-router";
import { getDevice, listDevices, type Device } from "@/api/basic";
import { getAddressRelations, type RelationNode } from "@/api/relations";
import { listDhcpRanges } from "@/api/integrations";
import RelationChain from "@/components/RelationChain.vue";
import SwitchPortLabel from "@/components/SwitchPortLabel.vue";
import { useScanProbes, probeLabel, osFamilyLabel } from "@/api/scanProbes";
import OsIcon from "@/components/OsIcon.vue";

const router = useRouter();
const { options: customerOptions, labelFor: customerLabelFor, ensureLoaded: ensureCustomersLoaded } = useCustomers();
const { isOld: isOldLog } = useChangeLogDim();
const devices = ref<Device[]>([]);

async function loadDevices() {
  if (devices.value.length) return;
  try {
    const r = await listDevices();
    devices.value = r.items;
  } catch { /* silent */ }
}

/** 確保這一台在名稱查得到的清單裡。
 *
 *  `listDevices()` 只拿一頁（200 筆），所以剛建立、或排序落在後面的裝置不會在裡面，
 *  名稱就會退回顯示一段 UUID。缺的那一台直接照 id 取回來即可 —— 不必也不該
 *  為了顯示一個名稱去把整個裝置清單抓完。 */
async function ensureDeviceLoaded(id: string | null | undefined) {
  if (!id || devices.value.some((d) => d.id === id)) return;
  try {
    devices.value = [...devices.value, await getDevice(id)];
  } catch { /* 沒權限或已刪除 —— 讓 deviceLabel 走它的退路 */ }
}

function deviceLabel(id: string | null | undefined): string {
  if (!id) return "—";
  return devices.value.find((d) => d.id === id)?.name ?? id.slice(0, 8) + "…";
}
const deviceOptions = computed(() =>
  devices.value.map((d) => ({ label: d.name, value: d.id })));

// DHCP 發放範圍：標示本 IP 是否落在某 DHCP 主機的動態池內
interface DhcpInfo { server: string; source: string; start: string; end: string; }
const dhcpRanges = ref<{ a: number; b: number; server: string; source: string; start: string; end: string }[]>([]);
function _ip2int(ip: string): number | null {
  const p = ip.trim().split(".");
  if (p.length !== 4) return null;
  let n = 0;
  for (const o of p) { const v = Number(o); if (!Number.isInteger(v) || v < 0 || v > 255) return null; n = n * 256 + v; }
  return n >>> 0;
}
async function loadDhcpRanges() {
  if (dhcpRanges.value.length) return;
  try {
    const rows = await listDhcpRanges();
    const out: typeof dhcpRanges.value = [];
    for (const r of rows) {
      const a = _ip2int(r.start_ip), b = _ip2int(r.end_ip);
      if (a != null && b != null) out.push({ a: Math.min(a, b), b: Math.max(a, b), server: r.source_name || "—", source: (r.source || "").toUpperCase(), start: r.start_ip, end: r.end_ip });
    }
    dhcpRanges.value = out;
  } catch { /* silent */ }
}
/** DHCP 固定分配的明細（後端在讀取端帶出來）。 */
const resv = computed<any | null>(() => (props.address as any)?.dhcp_reservation ?? null);
const resvLine = computed(() => {
  const r = resv.value;
  if (!r) return t("addresses.dhcp_reserved_tag");
  const parts = [r.mac, r.hostname, r.source_name ? `@${r.source_name}` : null]
    .filter(Boolean);
  return parts.length ? parts.join("　·　") : t("addresses.dhcp_reserved_tag");
});

const investigating = ref(false);

const dhcpInfo = computed<DhcpInfo | null>(() => {
  const ip = (props.address?.ip ?? "").split("/")[0];
  const n = ip ? _ip2int(ip) : null;
  if (n == null) return null;
  const r = dhcpRanges.value.find((x) => n >= x.a && n <= x.b);
  return r ? { server: r.server, source: r.source, start: r.start, end: r.end } : null;
});

const relations = ref<RelationNode[]>([]);
async function loadRelations() {
  relations.value = [];
  if (!props.address?.id) return;
  try { relations.value = await getAddressRelations(props.address.id); } catch { /* silent */ }
}

// 此 IP 被哪些 NAT 規則引用（src/dst）
const relatedNat = ref<{ id: string; name: string; type: string; src_interface: string | null;
  src_port: number | null; dst_port: number | null; source_label: string | null }[]>([]);
async function loadRelatedNat() {
  relatedNat.value = [];
  if (!props.address?.id) return;
  try {
    const { listNATs } = await import("@/api/phase3");
    const res = await listNATs({ ipId: props.address.id });
    relatedNat.value = res.items as any;
  } catch { /* silent */ }
}
// 此 IP 被哪些防火牆規則／別名管到（反向查詢）。
// 防火牆規則屬全域基礎設施資料：無 has_global_read 的帳號不打這支、也不顯示區塊。
const fwInfo = ref<{ rules: any[]; nat: any[]; aliases: any[] } | null>(null);
const auth = useAuthStore();
async function loadFirewall() {
  fwInfo.value = null;
  if (!props.address?.id) return;
  if (auth.me?.has_global_read === false) return;
  try {
    const { data } = await apiClient.get(`/api/v1/addresses/${props.address.id}/firewall`);
    fwInfo.value = data;
  } catch { /* silent — 沒有整合防火牆時就不顯示 */ }
}

function goNat() {
  emit("update:show", false);
  void router.push({ name: "nat" });
}

function goDevice(id: string | null | undefined) {
  if (!id) return;
  void router.push({ name: "device-detail", params: { id } });
}

// 後端原始值 → i18n 顯示；找不到 key 就回原值
function labelState(v: string | null | undefined): string {
  if (!v) return "—";
  const key = `addresses.state_${v}`;
  const out = t(key);
  return out === key ? v : out;
}
function labelSource(v: string | null | undefined): string {
  if (!v) return "—";
  const key = `addresses.source_${v}`;
  const out = t(key);
  return out === key ? v : out;
}
function labelEffective(v: string | null | undefined): string {
  if (!v) return "—";
  // 後端可能塞 "online (scanner)" 之類有附註的字串；只翻譯主詞
  const m = /^(\w+)(.*)$/.exec(v);
  if (!m) return v;
  const base = m[1].toLowerCase();
  let rest = m[2];
  // 來源附註本地化：(scanner) → (掃描代理)
  rest = rest.replace(/\(([^)]+)\)/g, (_full, src: string) => {
    const sk = `addresses.source_${src.trim().toLowerCase()}`;
    const sv = t(sk);
    return `(${sv === sk ? src : sv})`;
  });
  const key = `addresses.effective_${base}`;
  const out = t(key);
  return (out === key ? m[1] : out) + rest;
}

// 不偵測（exclude_from_ping）或 subnet 沒掃描時，後端的「離線」不該照搬 → 顯示未知，與狀態燈一致
const effectiveDisplay = computed(() => {
  const a = props.address;
  const v = a?.effective_status;
  if (!v) return "—";
  const noProbe = !!a?.exclude_from_ping || a?.subnet_scan_enabled === false;
  if (noProbe && /^offline/i.test(v)) return t("addresses.effective_unknown");
  return labelEffective(v);
});

const props = defineProps<{
  show: boolean;
  address: IPAddress | null;
  // create 模式：address 留 null，傳 createContext = { subnet_id, ip }
  createContext?: { subnet_id: string; ip: string } | null;
  // inline：當成獨立頁面內容渲染（不包 n-modal）；給 IPDetail 頁用
  inline?: boolean;
}>();

const emit = defineEmits<{
  (e: "update:show", v: boolean): void;
  (e: "saved", v: IPAddress): void;
  (e: "deleted", id: string): void;
  (e: "created", v: IPAddress): void;
  (e: "back"): void;
  (e: "ssh-open"): void;
  (e: "sftp-open"): void;
  (e: "ssh-popout"): void;
  (e: "rdp-open"): void;
  (e: "rdp-popout"): void;
  (e: "vnc-open"): void;
  (e: "vnc-popout"): void;
  (e: "novnc-open"): void;
  (e: "novnc-popout"): void;
  (e: "bmc-open"): void;
  (e: "bmc-popout"): void;
}>();

const { t, locale } = useI18n();
const msg = useMessage();
const { catalog } = useScanProbes();

// 此 IP 略過的探測項目（取代舊的單一 exclude_from_ping 開關）
const excludedProbes = ref<string[]>([]);

const editMode = ref(false);
const saving = ref(false);
const deleting = ref(false);

// 卡片寬度不足時，SSH/RDP/VNC 連線按鈕收成只有 icon（inline 詳細資料頁用）
const rootEl = ref<any>(null);
const consoleCompact = ref(false);
let cro: ResizeObserver | null = null;
onMounted(() => {
  void loadSuggestion();
  void ensureDeviceLoaded(props.address?.device_id);
  const el = (rootEl.value?.$el ?? rootEl.value) as HTMLElement | undefined;
  if (el instanceof HTMLElement) {
    cro = new ResizeObserver(() => { consoleCompact.value = el.clientWidth < 900; });
    cro.observe(el);
  }
});
onBeforeUnmount(() => { cro?.disconnect(); cro = null; });

const isCreate = computed(() => !props.address && !!props.createContext);

interface FormState {
  hostname: string;
  description: string;
  state: string;
  mac: string;
  owner: string;
  switch_port: string;
  ptr_ignore: boolean;
  note: string;
  customer_id: string | null;
  device_id: string | null;
  hostname_source_pin: string;  // "" = 自動 (跟全域優先序)
  ssh_enabled: boolean;
  sftp_enabled: boolean;
  rdp_enabled: boolean;
  vnc_enabled: boolean;
  novnc_enabled: boolean;
  bmc_enabled: boolean;
  is_dhcp_server: boolean;
}

const form = ref<FormState>(emptyForm());
// create 模式要填的 IP（FormState 不含 ip；新增時用此欄，可帶入 createContext.ip 預設值）
const createIp = ref("");

function emptyForm(): FormState {
  return {
    hostname: "", description: "", state: "active", mac: "",
    owner: "", switch_port: "",
    ptr_ignore: false, note: "",
    customer_id: null,
    device_id: null,
    hostname_source_pin: "",
    ssh_enabled: false,
    sftp_enabled: false,
    rdp_enabled: false,
    vnc_enabled: false,
    novnc_enabled: false,
    bmc_enabled: false,
    is_dhcp_server: false,
  };
}

// 目前編輯中的 IP（去 /prefix）；找名稱或管理 IP 等於本 IP、但尚未連結的裝置 → 一鍵關聯
const currentIpHost = computed(() => (props.address?.ip ?? "").split("/")[0].trim());
const matchingDevice = computed<Device | null>(() => {
  if (form.value.device_id) return null;
  const ip = currentIpHost.value;
  // 以「主機名稱」或「IP」找尚未連結的裝置：hostname=nas2 → 裝置 nas2
  const hn = (form.value.hostname || props.address?.hostname || "").trim().toLowerCase();
  if (!ip && !hn) return null;
  return devices.value.find((d) => {
    const dip = d.ip ? d.ip.split("/")[0].trim() : "";
    const dname = (d.name || "").trim().toLowerCase();
    const dfqdn = String((d as any).fqdn || "").trim().toLowerCase();
    return (!!ip && dip === ip)            // 裝置主要 IP == 本 IP
      || (!!hn && dname === hn)            // 裝置名稱 == 本 IP 主機名稱
      || (!!hn && !!dfqdn && dfqdn === hn) // 裝置 FQDN == 本 IP 主機名稱
      || (!!ip && dname === ip);           // 舊行為：裝置名稱剛好是 IP 字串
  }) ?? null;
});
// ── 「還沒有裝置」時的建議：由後端判斷（它看得到 MAC、連接埠、同名 IP 有幾筆）。
//    **只是建議** —— 不按就什麼都不會發生。
const suggestion = ref<DeviceSuggestion | null>(null);
const applyingSuggestion = ref(false);
/** 要一併關聯的其他 IP —— **預設只勾 MAC 相同的那些**。
 *
 *  同主機名稱不等於同一台機器：DHCP 把位址回收給別台之後，IP 記錄上的舊主機名稱
 *  還留著。實機上一台筆電的名字散在九筆 IP 上，裡面有 Proxmox 的 VM 和一顆 ESP32 ——
 *  「全部掛上」那種開關等於把猜測當成事實。 */
const linkIds = ref<string[]>([]);

async function loadSuggestion() {
  suggestion.value = null;
  if (isCreate.value || !props.address?.id || form.value.device_id) return;
  try {
    suggestion.value = await getDeviceSuggestion(props.address.id);
    linkIds.value = (suggestion.value?.siblings ?? [])
      .filter((s) => s.same_mac).map((s) => s.id);
  } catch { /* 建議缺了不影響編輯 */ }
}

/** 按下去才會建立／關聯。建完直接反映在表單上，不用再存一次。 */
async function applySuggestion(create: boolean) {
  const sug = suggestion.value;
  if (!sug || !props.address?.id) return;
  applyingSuggestion.value = true;
  try {
    const updated = await applyDeviceSuggestion(props.address.id, {
      ...(create ? { create_name: sug.suggested_name ?? "" }
                 : { device_id: sug.existing_device_id ?? "" }),
      link_ip_ids: linkIds.value,
    });
    form.value.device_id = updated.device_id ?? null;
    suggestion.value = null;
    // 剛建出來的那一台不會在既有清單裡 —— 直接補進去，否則畫面只顯示得出 UUID
    await ensureDeviceLoaded(updated.device_id);
    emit("saved", updated);
  } catch (e: any) {
    msg.error(e?.response?.data?.detail ?? String(e));
  } finally {
    applyingSuggestion.value = false;
  }
}

async function linkMatchingDevice() {
  if (!matchingDevice.value) return;
  form.value.device_id = matchingDevice.value.id;
  // 一鍵關聯即存：避免使用者漏按儲存、或編輯狀態問題導致「關聯沒生效」
  if (!isCreate.value && props.address) await save();
}

function fromAddress(a: IPAddress): FormState {
  return {
    hostname: a.hostname ?? "",
    description: a.description ?? "",
    state: a.state ?? "active",
    mac: a.mac ?? "",
    owner: a.owner ?? "",
    switch_port: a.switch_port ?? "",
    ptr_ignore: !!a.ptr_ignore,
    note: a.note ?? "",
    customer_id: a.customer_id ?? null,
    device_id: (a as any).device_id ?? null,
    hostname_source_pin: a.hostname_source_pin ?? "",
    ssh_enabled: !!a.ssh_enabled,
    sftp_enabled: !!a.sftp_enabled,
    rdp_enabled: !!a.rdp_enabled,
    vnc_enabled: !!a.vnc_enabled,
    novnc_enabled: !!a.novnc_enabled,
    bmc_enabled: !!a.bmc_enabled,
    is_dhcp_server: !!a.is_dhcp_server,
  };
}

const stateOptions = computed(() => [
  { label: labelState("active"), value: "active" },
  { label: labelState("reserved"), value: "reserved" },
  { label: labelState("offline"), value: "offline" },
  { label: labelState("dhcp"), value: "dhcp" },
  { label: labelState("used"), value: "used" },
  { label: labelState("deprecated"), value: "deprecated" },
  { label: labelState("quarantine"), value: "quarantine" },
]);

watch(
  () => [props.show, props.address?.id, props.createContext?.ip],
  () => {
    // create 模式自動進 edit form；既有 IP 進 view
    editMode.value = isCreate.value;
    form.value = props.address ? fromAddress(props.address) : emptyForm();
    createIp.value = (props.createContext?.ip ?? "").trim();
    // 略過探測初始化：優先用 excluded_probes；空但舊 exclude_from_ping=true → 回填 ['icmp']
    const a = props.address;
    if (a) {
      const ex = Array.isArray(a.excluded_probes) ? [...a.excluded_probes] : [];
      excludedProbes.value = ex.length ? ex : (a.exclude_from_ping ? ["icmp"] : []);
    } else {
      excludedProbes.value = [];
    }
    if (props.show) {
      void ensureCustomersLoaded();
      void loadDevices();
      void loadDhcpRanges();
      void loadRelations();
      void loadRelatedNat();
      void loadFirewall();
    }
  },
  { immediate: true },
);

const stateType = computed<"success" | "info" | "warning" | "error" | "default">(() => {
  const s = props.address?.state ?? "active";
  return s === "active" ? "success"
       : s === "reserved" ? "info"
       : s === "offline" ? "error"
       : s === "dhcp" ? "warning"
       : "default";
});

// ── 異動記錄：展開時才載入。**分頁 + 篩選 + 總數** ──
// 實機單一 IP 最多 1,838 筆、且幾乎全是同一種事件（整合互相覆寫主機名稱），
// 原本的「載入更多」要按 18 次、也看不出總共幾筆 → 改成先篩選再翻頁。
const HISTORY_PAGE = 50;
const history = ref<IPChangeLog[]>([]);
const historyTotal = ref(0);
const historyPage = ref(1);
const historyEventOpts = ref<HistoryFacet[]>([]);
const historySourceOpts = ref<HistoryFacet[]>([]);
const historyEvent = ref<string | null>(null);
const historySource = ref<string | null>(null);
const historyLoading = ref(false);
const historyLoaded = ref(false);
const historyPageCount = computed(() =>
  Math.max(1, Math.ceil(historyTotal.value / HISTORY_PAGE)));

async function loadHistory() {
  if (!props.address?.id) return;
  historyLoading.value = true;
  try {
    const page = await getAddressHistory(props.address.id, {
      limit: HISTORY_PAGE,
      offset: (historyPage.value - 1) * HISTORY_PAGE,
      event_type: historyEvent.value ?? undefined,
      source: historySource.value ?? undefined,
    });
    history.value = page.items;
    historyTotal.value = page.total;
    // 選項以「未篩選」為母體，選了之後其他選項不會消失
    historyEventOpts.value = page.event_types;
    historySourceOpts.value = page.sources;
    historyLoaded.value = true;
  } catch { /* silent */ } finally {
    historyLoading.value = false;
  }
}

function onHistoryFilter() {
  historyPage.value = 1;      // 換篩選條件一律回第一頁，否則會停在不存在的頁
  void loadHistory();
}

function onHistoryToggle(names: Array<string | number>) {
  if (names.includes("history") && !historyLoaded.value) void loadHistory();
}

/** 防火牆逐來源證據，時間新的排前面。 */
const fwSeen = computed<[string, string][]>(() => {
  const raw = (props.address as any)?.arp_seen as Record<string, string> | undefined;
  if (!raw) return [];
  return Object.entries(raw).sort((a, b) => (a[1] < b[1] ? 1 : -1));
});

const FW_VENDOR: Record<string, string> = {
  opnsense: "OPNsense", pfsense: "pfSense", fortigate: "FortiGate",
  paloalto: "Palo Alto", librenms: "LibreNMS",
};

/** `arp:opnsense` → 「ARP 表（OPNsense）」 */
function fwSeenLabel(key: string): string {
  const [kind, vendor] = key.split(":", 2);
  if (!vendor) return key;
  return `${t(`system_settings.src_kind_${kind}`)}（${FW_VENDOR[vendor] ?? vendor}）`;
}

function eventLabel(e: string): string {
  const key = `ipChanges.event.${e}`;
  const out = t(key);
  return out === key ? e : out;
}

const HISTORY_TYPE: Record<string, "default" | "info" | "success" | "warning" | "error"> = {
  created: "success", deleted: "error", online: "success", offline: "warning",
  hostname_changed: "info", mac_changed: "info", arp_changed: "info",
  state_changed: "warning", edited: "default",
};

// ── hostname 多來源 (feature A)：開 modal 時載入，給 pin 下拉用 ──
const hostnameSources = ref<HostnameSources | null>(null);
const hostnameSourcesLoaded = ref(false);

async function loadHostnameSources() {
  if (hostnameSourcesLoaded.value || !props.address?.id) return;
  try {
    hostnameSources.value = await getHostnameSources(props.address.id);
    hostnameSourcesLoaded.value = true;
  } catch { /* silent */ }
}

// pin 下拉選項：auto + 有觀測的來源 (顯示該來源回報的 hostname)
const pinOptions = computed(() => {
  const opts: Array<{ label: string; value: string }> = [
    { label: t("hostnameSrc.auto"), value: "" },
  ];
  for (const o of hostnameSources.value?.observations ?? []) {
    opts.push({ label: `${labelSource(o.source)} — ${o.hostname}`, value: o.source });
  }
  return opts;
});

// 交換器位置：儲存格式是「交換器 / 埠」（LibreNMS 同步就是這樣寫），
// 顯示與編輯都以 @ 呈現。這裡把單一字串拆成兩格、存檔前再組回去。
const swName = ref("");
const swPort = ref("");
function splitSwitchPort(v: string): [string, string] {
  const i = (v || "").indexOf(" / ");
  return i < 0 ? [v || "", ""] : [v.slice(0, i), v.slice(i + 3)];
}
watch(() => form.value.switch_port, (v) => {
  const [a, b] = splitSwitchPort(v || "");
  if (a !== swName.value) swName.value = a;
  if (b !== swPort.value) swPort.value = b;
}, { immediate: true });
watch([swName, swPort], ([a, b]) => {
  const name = (a || "").trim();
  const port = (b || "").trim();
  form.value.switch_port = port ? `${name} / ${port}` : name;
});

// 換 IP 時清掉舊快取
watch(() => props.address?.id, () => {
  void loadSuggestion();
  void ensureDeviceLoaded(props.address?.device_id);
  history.value = [];
  historyLoaded.value = false;
  historyTotal.value = 0;
  historyPage.value = 1;
  historyEvent.value = null;
  historySource.value = null;
  hostnameSources.value = null;
  hostnameSourcesLoaded.value = false;
  switchPort.value = null;
});

// FDB 推得的 switch port(feature E)
const switchPort = ref<SwitchPortInfo | null>(null);
async function loadSwitchPort() {
  if (!props.address?.id) return;
  try { switchPort.value = await getAddressSwitchPort(props.address.id); }
  catch { switchPort.value = null; }
}

// immediate：inline（IP 詳細資料頁）模式下 show 一開始就是 true、id 一開始就有值，
// 沒有 immediate 這個 watch 永遠不觸發 → 直接開網址／重新整理時「主機名稱來源」
// 與 FDB 標籤整列消失，只有從清單開彈窗（show false→true）才看得到（使用者回報）。
watch(() => [props.show, props.address?.id], () => {
  if (props.show && props.address?.id) { void loadHostnameSources(); void loadSwitchPort(); }
}, { immediate: true });

function close() {
  // inline(頁面)模式：檢視中按取消＝返回上一頁；編輯中＝退出編輯
  if (props.inline) {
    if (editMode.value && !isCreate.value) { editMode.value = false; return; }
    emit("back");
    return;
  }
  emit("update:show", false);
}

async function save() {
  saving.value = true;
  try {
    if (isCreate.value && props.createContext) {
      const ipv = createIp.value.trim();
      if (!ipv) { msg.warning(t("addresses.ip_required")); saving.value = false; return; }
      const created = await createAddress({
        subnet_id: props.createContext.subnet_id,
        ip: ipv,
        hostname: form.value.hostname.trim() || null,
        description: form.value.description.trim() || null,
        state: form.value.state,
        mac: form.value.mac.trim() || null,
        owner: form.value.owner.trim() || null,
        switch_port: form.value.switch_port.trim() || null,
        note: form.value.note.trim() || null,
        customer_id: form.value.customer_id ?? null,
        device_id: form.value.device_id ?? null,
      });
      msg.success(t("common.ok"));
      emit("created", created);
      emit("update:show", false);
      return;
    }
    if (!props.address) return;
    const payload: IPAddressUpdate = {
      hostname: form.value.hostname.trim() || null,
      description: form.value.description.trim() || null,
      state: form.value.state,
      mac: form.value.mac.trim() || null,
      owner: form.value.owner.trim() || null,
      switch_port: form.value.switch_port.trim() || null,
      excluded_probes: excludedProbes.value,
      ptr_ignore: form.value.ptr_ignore,
      note: form.value.note.trim() || null,
      customer_id: form.value.customer_id ?? null,
      device_id: form.value.device_id ?? null,
      hostname_source_pin: form.value.hostname_source_pin || null,
      ssh_enabled: form.value.ssh_enabled,
      sftp_enabled: form.value.sftp_enabled,
      rdp_enabled: form.value.rdp_enabled,
      vnc_enabled: form.value.vnc_enabled,
      novnc_enabled: form.value.novnc_enabled,
      bmc_enabled: form.value.bmc_enabled,
      is_dhcp_server: form.value.is_dhcp_server,
    };
    const updated = await updateAddress(props.address?.id, payload);
    hostnameSourcesLoaded.value = false;  // 重新整理來源/有效 hostname
    msg.success(t("common.ok"));
    emit("saved", updated);
    editMode.value = false;
  } catch (e: any) {
    msg.error(e?.response?.data?.detail ?? t("errors.network"));
  } finally {
    saving.value = false;
  }
}

async function remove() {
  if (!props.address) return;
  deleting.value = true;
  try {
    const id = props.address?.id;
    await deleteAddress(id);
    msg.success(t("common.ok"));
    emit("deleted", id);
    close();
  } catch (e: any) {
    msg.error(e?.response?.data?.detail ?? t("errors.network"));
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <component :is="inline ? 'div' : NModal" ref="rootEl" v-bind="inline ? {} : { show: props.show, 'onUpdate:show': (v: boolean) => emit('update:show', v) }">
    <n-card
      :style="inline ? 'width: 100%' : 'width: 880px; max-width: 95vw'"
      :bordered="false"
      :role="inline ? undefined : 'dialog'"
      :aria-modal="inline ? undefined : 'true'"
    >
      <!-- 標題：IP + 狀態標籤並排（比照裝置詳細資料的 名稱+類型標籤）-->
      <template #header>
        <span style="display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span>{{ props.address?.ip ?? props.createContext?.ip ?? '' }}</span>
          <n-tag v-if="isCreate" type="info" size="small">{{ t("common.create") }}</n-tag>
          <n-tag v-else :type="stateType" size="small">{{ labelState(props.address?.state) }}</n-tag>
          <!-- 「真的有 DHCP 租約」與「只是落在 DHCP 池範圍內」是兩回事：
               後者常見於在池範圍內設固定 IP 的機器，標成 DHCP 會誤導，改用中性的「DHCP 範圍」。 -->
          <!-- 固定分配（DHCP reservation）：這個位址被綁給某張網卡，不會被回收給別台。
               與上面的「DHCP／DHCP 範圍」是不同的事實，所以分開標。 -->
          <n-tooltip v-if="props.address?.dhcp_reserved" :delay="0">
            <template #trigger>
              <n-tag type="success" size="small" :bordered="false">
                {{ t("addresses.dhcp_reserved_tag") }}
              </n-tag>
            </template>
            <div style="max-width:300px;line-height:1.5">
              <div>{{ t("addresses.dhcp_reserved_hint") }}</div>
              <template v-if="resv">
                <div v-if="resv.mac" style="margin-top:4px">
                  <strong>{{ t("addresses.dhcp_reserved_mac") }}：</strong>{{ resv.mac }}
                </div>
                <div v-if="resv.source_name">
                  <strong>{{ t("addresses.dhcp_server") }}：</strong>{{ resv.source_name }}
                  <span v-if="resv.engine">（{{ resv.engine }}）</span>
                </div>
                <div v-if="resv.hostname">{{ t("addresses.hostname") }}：{{ resv.hostname }}</div>
              </template>
            </div>
          </n-tooltip>
          <n-tooltip v-if="dhcpInfo || props.address?.in_dhcp_lease" :delay="0">
            <template #trigger>
              <n-tag :type="props.address?.in_dhcp_lease ? 'warning' : 'default'" size="small" :bordered="false">
                {{ props.address?.in_dhcp_lease ? "DHCP" : t("addresses.dhcp_in_range_tag") }}
              </n-tag>
            </template>
            <div style="max-width:280px;line-height:1.5">
              <div v-if="props.address?.in_dhcp_lease">{{ t("addresses.dhcp_has_lease") }}</div>
              <template v-if="dhcpInfo">
                <div>{{ t("addresses.dhcp_pool_hint") }}</div>
                <div v-if="!props.address?.in_dhcp_lease" style="margin-top:4px">{{ t("addresses.dhcp_no_lease_hint") }}</div>
                <div style="margin-top:4px"><strong>{{ t("addresses.dhcp_server") }}：</strong>{{ dhcpInfo.server }}{{ dhcpInfo.source ? ` (${dhcpInfo.source})` : "" }}</div>
                <div>{{ t("addresses.dhcp_range") }}：{{ dhcpInfo.start }} – {{ dhcpInfo.end }}</div>
              </template>
            </div>
          </n-tooltip>
        </span>
      </template>
      <!-- inline(頁面)模式：操作工具列自標題列搬到內文最上方 -->
      <n-space v-if="inline && !isCreate" align="center" justify="end" :size="8" :wrap-item="false"
               style="margin-bottom: 10px">
          <template v-if="!editMode">
            <!-- SSH 連線分割按鈕：主鍵嵌入終端機、下箭頭可另開視窗（僅在啟用且有權限時顯示） -->
            <template v-if="props.address?.ssh_available">
              <n-tooltip :delay="200">
                <template #trigger>
                  <n-button-group key="hx-ssh">
                    <n-button type="info" size="small" @click="emit('ssh-open')">
                      <template #icon><n-icon><TerminalIcon /></n-icon></template>
                      <span v-if="!consoleCompact">{{ t("ssh.connect") }}</span>
                    </n-button>
                  </n-button-group>
                </template>
                {{ t("ssh.connect") }}
              </n-tooltip>
            </template>
            <!-- SFTP：自己的開關（sftp_enabled）與自己的權限判定，與 SSH 各自獨立顯示 ——
                 有些主機只想開放傳檔、不想開終端機。分成兩顆而不是塞進同一顆的下拉：
                 上下傳檔案跟開終端機是兩件不同的事，要用的人一開始就知道自己要哪一個。 -->
            <template v-if="props.address?.sftp_available">
              <n-tooltip :delay="200">
                <template #trigger>
                  <n-button-group key="hx-sftp">
                    <n-button size="small" @click="emit('sftp-open')">
                      <template #icon><n-icon><FilesIcon /></n-icon></template>
                      <span v-if="!consoleCompact">{{ t("sftp.connect_btn") }}</span>
                    </n-button>
                  </n-button-group>
                </template>
                {{ t("sftp.connect_hint") }}
              </n-tooltip>
            </template>
            <!-- RDP 連線分割按鈕：主鍵新分頁、下箭頭另開視窗（僅在啟用且有權限時顯示） -->
            <span v-if="props.address?.rdp_available" key="hx-rdp" class="conn-beta-wrap">
              <n-tooltip :delay="200">
                <template #trigger>
                  <n-button-group>
                    <n-button type="info" size="small" @click="emit('rdp-open')">
                      <template #icon><n-icon><DisplayIcon /></n-icon></template>
                      <span v-if="!consoleCompact">{{ t("rdp.connect") }}</span>
                    </n-button>
                  </n-button-group>
                </template>
                {{ t("rdp.connect") }}
              </n-tooltip>
              <span class="conn-beta-badge">{{ t("rdp.beta") }}</span>
            </span>
            <!-- VNC 連線分割按鈕：主鍵新分頁、下箭頭另開視窗（僅在啟用且有權限時顯示） -->
            <span v-if="props.address?.vnc_available" key="hx-vnc" class="conn-beta-wrap">
              <n-tooltip :delay="200">
                <template #trigger>
                  <n-button-group>
                    <n-button type="info" size="small" @click="emit('vnc-open')">
                      <template #icon><n-icon><VncIcon /></n-icon></template>
                      <span v-if="!consoleCompact">{{ t("vnc.connect") }}</span>
                    </n-button>
                  </n-button-group>
                </template>
                {{ t("vnc.connect") }}
              </n-tooltip>
              <span class="conn-beta-badge">{{ t("vnc.beta") }}</span>
            </span>
            <!-- PVE 主控台連線按鈕（noVNC/xterm；僅在該 IP 是 PVE VM/CT 且有權限時顯示），右上小標 PVE -->
            <span v-if="props.address?.novnc_available" key="hx-novnc" class="conn-beta-wrap">
              <n-tooltip :delay="200">
                <template #trigger>
                  <n-button-group>
                    <n-button type="warning" size="small" @click="emit('novnc-open')">
                      <template #icon>
                        <n-icon><TerminalIcon v-if="props.address?.pve?.kind === 'ct'" /><NoVncIcon v-else /></n-icon>
                      </template>
                      <span v-if="!consoleCompact">{{ props.address?.pve?.kind === 'ct' ? 'xterm' : 'noVNC' }}</span>
                    </n-button>
                  </n-button-group>
                </template>
                {{ `${props.address?.pve?.kind === 'ct' ? 'xterm' : 'noVNC'} ${t('novnc.connect')}` }}
              </n-tooltip>
              <span class="conn-beta-badge conn-pve-badge">PVE</span>
            </span>
            <!-- BMC 主控台連線按鈕（IPMI SOL；該 IP 啟用 BMC 且有權限時顯示），右上小標 SOL -->
            <span v-if="props.address?.bmc_available" key="hx-bmc" class="conn-beta-wrap">
              <n-tooltip :delay="200">
                <template #trigger>
                  <n-button-group>
                    <n-button type="warning" size="small" @click="emit('bmc-open')">
                      <template #icon><n-icon><TerminalIcon /></n-icon></template>
                      <span v-if="!consoleCompact">BMC</span>
                    </n-button>
                  </n-button-group>
                </template>
                {{ t("bmc.connect") }}
              </n-tooltip>
              <span class="conn-beta-badge conn-sol-badge">SOL</span>
            </span>
            <!-- 連線鈕（SSH/RDP/VNC/PVE/BMC）與編輯/刪除間只留一條分隔線 -->
            <n-divider v-if="props.address?.ssh_available || props.address?.sftp_available || props.address?.rdp_available || props.address?.vnc_available || props.address?.novnc_available || props.address?.bmc_available"
                       key="hx-conn-div" vertical />
            <!-- 調查：把這個位址散在各處的線索收在一起（追問題時最花時間的就是到處翻） -->
            <n-button key="hx-inv" size="small" @click="investigating = true">
              <template #icon><n-icon><SearchIcon /></n-icon></template>{{ t("investigate.title") }}
            </n-button>
            <n-button key="hx-edit" type="primary" size="small" @click="editMode = true">
              <template #icon><n-icon><EditIcon /></n-icon></template>{{ t("common.edit") }}
            </n-button>
            <n-popconfirm key="hx-del-view" @positive-click="remove">
              <template #trigger>
                <n-button type="error" ghost size="small" :loading="deleting">
                  <template #icon><n-icon><DeleteIcon /></n-icon></template>{{ t("common.delete") }}
                </n-button>
              </template>
              {{ t("common.confirm_delete") }}
            </n-popconfirm>
            <n-button key="hx-back" size="small" @click="emit('back')">
              <template #icon><n-icon><ArrowLeftIcon /></n-icon></template>{{ t("common.back") }}
            </n-button>
          </template>
          <template v-else>
            <n-popconfirm key="hx-del-edit" @positive-click="remove">
              <template #trigger>
                <n-button type="error" ghost size="small" :loading="deleting">
                  <template #icon><n-icon><DeleteIcon /></n-icon></template>{{ t("common.delete") }}
                </n-button>
              </template>
              {{ t("common.confirm_delete") }}
            </n-popconfirm>
            <n-button key="hx-cancel" size="small" @click="close">
              <template #icon><n-icon><CancelIcon /></n-icon></template>{{ t("common.cancel") }}
            </n-button>
            <n-button key="hx-save" type="success" size="small" :loading="saving" @click="save">
              <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
            </n-button>
          </template>
      </n-space>

      <div v-if="props.address || isCreate">
        <!-- view mode -->
        <n-descriptions v-if="!editMode" bordered :column="2" size="small" label-placement="left"
                        label-align="left"
                        :label-style="{ width: '132px', whiteSpace: 'nowrap', verticalAlign: 'top' }"
                        :content-style="{ verticalAlign: 'top', wordBreak: 'break-word', minWidth: '160px' }">
          <n-descriptions-item :label="t('addresses.ip')">{{ props.address?.ip }}</n-descriptions-item>
          <n-descriptions-item :label="t('common.status')">
            {{ labelState(props.address?.state) }}
            <!-- 角色旗標（閘道 / DHCP 伺服器 / 在 DHCP 範圍內…）清單頁早就顯示了，
                 詳細資料頁卻看不到 —— 同一個 IP 在兩個畫面資訊不一致很容易誤判。 -->
            <IpRoleTags v-if="props.address" :row="props.address" style="margin-left:8px" />
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.hostname')">
            <span>{{ props.address?.hostname ?? "—" }}</span>
            <n-tag v-if="hostnameSources?.pin" size="tiny" type="warning" :bordered="false"
                   style="margin-left: 6px">{{ t("hostnameSrc.pinned", { src: hostnameSources.pin }) }}</n-tag>
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.mac')">
            <span>{{ props.address?.mac ?? "—" }}</span>
            <n-tag v-if="props.address?.mac_vendor" size="tiny" type="info" bordered
                   style="margin-left: 6px">{{ props.address.mac_vendor }}</n-tag>
          </n-descriptions-item>
          <n-descriptions-item :label="t('cols.os')">
            <n-tooltip v-if="props.address?.os_family" :disabled="!props.address?.os_guess">
              <template #trigger>
                <span style="display:inline-flex;align-items:center;gap:6px">
                  <os-icon :family="props.address.os_family" :size="16" />
                  <span>{{ osFamilyLabel(catalog.os_families, props.address.os_family, locale) }}</span>
                  <span v-if="props.address?.os_source" style="opacity:0.6;font-size:0.85em">
                    {{ "（" + t("os_precedence.source_label") + ": " + t("os_precedence.src_" + props.address.os_source) + "）" }}
                  </span>
                </span>
              </template>
              {{ props.address?.os_guess }}
            </n-tooltip>
            <span v-else>—</span>
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.owner')">{{ props.address?.owner ?? "—" }}</n-descriptions-item>
          <n-descriptions-item :label="t('addresses.switch_port')">
            <template v-if="props.address?.switch_port">
              <n-tooltip v-if="props.address?.switch_port_confident === false">
                <template #trigger>
                  <switch-port-label :value="props.address.switch_port" dim />
                </template>
                {{ t("addresses.switch_port_uncertain") }}
              </n-tooltip>
              <switch-port-label v-else :value="props.address.switch_port" />
            </template>
            <span v-else>—</span>
            <n-tag v-if="switchPort?.likely_access_port?.port" size="tiny" type="info"
                   style="margin-left: 6px">
              FDB: <switch-port-label :value="`${switchPort.likely_access_port.switch} / ${switchPort.likely_access_port.port}`" />
            </n-tag>
          </n-descriptions-item>
          <n-descriptions-item :label="t('nav.devices')" :span="2">
            <a v-if="props.address?.device_id" href="#"
               style="color: var(--primary-color, #18a058); text-decoration: none;"
               @click.prevent="goDevice(props.address?.device_id)">
              {{ deviceLabel(props.address?.device_id) }}
            </a>
            <span v-else>—</span>
          </n-descriptions-item>
          <n-descriptions-item
            v-if="hostnameSources && hostnameSources.observations.length"
            :label="t('hostnameSrc.sources')" :span="2"
          >
            <n-space :size="6" style="flex-wrap: wrap">
              <!-- 純顯示，不提供刪除：這裡是「各來源分別回報了什麼」的觀測記錄，
                   實際採用哪一個由主機名稱優先序決定。給一個 X 會讓人以為要在這裡挑，
                   而且刪掉之後下次同步又會回來。 -->
              <n-tag
                v-for="o in hostnameSources.observations" :key="o.source"
                size="small" :bordered="false"
                :type="o.hostname === props.address?.hostname ? 'success' : 'default'"
              >
                {{ labelSource(o.source) }}: {{ o.hostname }}
              </n-tag>
            </n-space>
          </n-descriptions-item>
          <n-descriptions-item :label="t('common.description')" :span="2">
            {{ props.address?.description ?? "—" }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.note')" :span="2">
            {{ props.address?.note ?? "—" }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('nav.customers')" :span="2">
            {{ customerLabelFor(props.address?.customer_id) }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.exclude_from_ping')">{{ props.address?.exclude_from_ping ? "✓" : "—" }}</n-descriptions-item>
          <n-descriptions-item :label="t('addresses.ptr_ignore')">{{ props.address?.ptr_ignore ? "✓" : "—" }}</n-descriptions-item>
          <n-descriptions-item :label="t('addresses.source')">{{ labelSource(props.address?.discovery_source) }}</n-descriptions-item>
          <!-- 有固定分配才顯示這一列：沒有的話多一列「否」只是噪音 -->
          <n-descriptions-item v-if="props.address?.dhcp_reserved" :label="t('addresses.dhcp_reserved_tag')">
            {{ resvLine }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.effective_status')">{{ effectiveDisplay }}</n-descriptions-item>
          <!-- 虛擬化對應：比對到 VM 網卡才顯示「虛擬機」；比對不到不顯示——
               「查無」是「不知道」，不是「實體機」，反向斷言是誤導 -->
          <n-descriptions-item v-if="(props.address as any)?.virt_vm" :label="t('addresses.virt')">
            <n-tag size="small" type="info">{{ t('addresses.virt_vm_tag') }}</n-tag>
            <span style="margin-left:6px">{{ (props.address as any).virt_vm.vm }}
              <span style="opacity:.65">@ {{ (props.address as any).virt_vm.cluster || (props.address as any).virt_vm.platform }}</span></span>
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.last_seen_scanner')">{{ fmtDateTime(props.address?.last_seen_scanner) }}</n-descriptions-item>
          <n-descriptions-item :label="t('addresses.last_seen_librenms')">{{ fmtDateTime(props.address?.last_seen_librenms) }}</n-descriptions-item>
          <n-descriptions-item :label="t('addresses.last_seen_arp')">
            {{ fmtDateTime(props.address?.last_seen_arp) }}
            <n-tooltip v-if="props.address?.last_seen_arp" trigger="hover">
              <template #trigger><span class="arp-caveat">?</span></template>
              <div style="max-width: 300px">{{ t("live_dot.arp_only_hint") }}</div>
            </n-tooltip>
          </n-descriptions-item>
          <n-descriptions-item v-if="props.address?.last_seen_wazuh"
                               :label="t('addresses.last_seen_wazuh')">
            {{ fmtDateTime(props.address?.last_seen_wazuh) }}
          </n-descriptions-item>
          <!-- 防火牆給的證據逐來源列出。以前這些全被寫成「掃描代理」，於是沒有代理的站台
               也看得到掃描代理的時間 —— 現在照實顯示是哪一台防火牆、哪一種表看到的。 -->
          <n-descriptions-item v-for="[k, v] in fwSeen" :key="k" :label="fwSeenLabel(k)">
            {{ fmtDateTime(v) }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('addresses.last_seen_dns')">{{ fmtDateTime(props.address?.last_seen_dns) }}</n-descriptions-item>
          <n-descriptions-item :label="t('common.created_at')">{{ fmtDateTime(props.address?.created_at) }}</n-descriptions-item>
          <n-descriptions-item :label="t('common.updated_at')" :span="2">{{ fmtDateTime(props.address?.updated_at) }}</n-descriptions-item>
        </n-descriptions>

        <!-- 上下關係鏈：區段 → 子網路 → 位址 → 裝置 → 機櫃 → 機房 -->
        <div v-if="!editMode && relations.length > 1" style="margin-top: 14px">
          <div style="font-size: 12px; opacity: 0.6; margin-bottom: 6px">{{ t("relations.title") }}</div>
          <relation-chain :nodes="relations" :current-id="props.address?.id" />
        </div>

        <!-- 防火牆反查：這個 IP 被哪些規則／別名管到（any 規則不列，另以一句話註明） -->
        <div v-if="!editMode && fwInfo && (fwInfo.rules.length || fwInfo.aliases.length)"
             style="margin-top: 14px">
          <div style="font-size: 12px; opacity: 0.6; margin-bottom: 6px">
            {{ t("addresses.fw_title", { n: fwInfo.rules.length }) }}
          </div>
          <div v-for="(r, i) in fwInfo.rules.slice(0, 12)" :key="i"
               style="font-size: 12.5px; line-height: 1.8">
            <n-tag size="tiny" style="margin-right: 6px">{{ r.source_type }}</n-tag>
            {{ r.firewall }}｜{{ r.action }} {{ r.src }} → {{ r.dst }}{{ r.dst_port ? ":" + r.dst_port : "" }}
            <span style="opacity:.6">（{{ r.match }}{{ r.descr ? "；" + r.descr : "" }}）</span>
          </div>
          <div v-if="fwInfo.aliases.length" style="font-size: 12.5px; margin-top: 4px">
            {{ t("addresses.fw_aliases") }}：
            <n-tag v-for="a in fwInfo.aliases" :key="a.name" size="tiny" style="margin-right: 4px">
              {{ a.name }}（{{ a.source_type }}）
            </n-tag>
          </div>
          <div style="font-size: 11.5px; opacity: 0.55; margin-top: 4px">{{ t("addresses.fw_any_note") }}</div>
        </div>

        <!-- 關聯的 NAT 規則 -->
        <div v-if="!editMode && relatedNat.length" style="margin-top: 14px">
          <div style="font-size: 12px; opacity: 0.6; margin-bottom: 6px">
            {{ t("addresses.related_nat", { n: relatedNat.length }) }}
          </div>
          <n-space vertical :size="6">
            <div v-for="n in relatedNat" :key="n.id" class="nat-ref" @click="goNat">
              <n-tag size="small" type="info" :bordered="false">{{ n.type }}</n-tag>
              <span class="nat-ref-name">{{ n.name }}</span>
              <span class="nat-ref-meta">
                <template v-if="n.src_interface">{{ n.src_interface }}</template>
                <template v-if="n.dst_port"> · :{{ n.dst_port }}</template>
              </span>
              <n-tag v-if="n.source_label" size="tiny" :bordered="false">{{ n.source_label }}</n-tag>
            </div>
          </n-space>
        </div>

        <!-- 異動記錄 (feature B)，展開才載入 -->
        <n-collapse v-if="!editMode && props.address" style="margin-top: 12px" @update:expanded-names="onHistoryToggle">
          <n-collapse-item name="history">
            <!-- 標題帶總數：不然使用者不知道自己看到的是全部還是一頁 -->
            <template #header>
              {{ t("ipChanges.history") }}
              <n-text v-if="historyLoaded" depth="3" style="font-size: 12.5px">
                （{{ historyTotal }}）
              </n-text>
            </template>
            <n-spin :show="historyLoading">
              <!-- 篩選：整合互相覆寫會讓單一事件類型灌爆清單，先篩再看才有意義 -->
              <n-space v-if="historyLoaded && (historyEventOpts.length > 1 || historySourceOpts.length > 1)"
                       :size="8" style="margin-bottom: 8px">
                <n-select v-model:value="historyEvent" clearable size="small" style="width: 190px"
                          :placeholder="t('ipChanges.all_events')"
                          :options="historyEventOpts.map((o) => ({
                            label: `${eventLabel(o.value)}（${o.count}）`, value: o.value }))"
                          @update:value="onHistoryFilter" />
                <n-select v-model:value="historySource" clearable size="small" style="width: 190px"
                          :placeholder="t('ipChanges.all_sources')"
                          :options="historySourceOpts.map((o) => ({
                            label: `${labelSource(o.value)}（${o.count}）`, value: o.value }))"
                          @update:value="onHistoryFilter" />
              </n-space>
              <n-empty v-if="historyLoaded && !history.length" :description="t('ipChanges.empty')" size="small" />
              <n-timeline v-else style="padding: 4px 0">
                <n-timeline-item
                  v-for="h in history" :key="h.id"
                  :type="HISTORY_TYPE[h.event_type] ?? 'default'"
                  :time="fmtDateTime(h.created_at)"
                  :class="{ 'log-dim': isOldLog(h.created_at) }"
                >
                  <template #header>
                    <n-space align="center" :size="6">
                      <strong>{{ eventLabel(h.event_type) }}</strong>
                      <n-tag size="tiny" :bordered="false">{{ labelSource(h.source) }}</n-tag>
                      <n-text v-if="h.actor_username" depth="3" style="font-size: 12px">{{ h.actor_username }}</n-text>
                    </n-space>
                  </template>
                  <n-text v-if="h.old_value != null || h.new_value != null" style="font-size: 13px">
                    <span v-if="h.field">{{ h.field }}: </span>
                    <n-text depth="3" delete><ChangeValue :field="h.field" :value="h.old_value" /></n-text>
                    →
                    <n-text strong><ChangeValue :field="h.field" :value="h.new_value" /></n-text>
                  </n-text>
                  <n-text v-if="h.note" depth="3" style="font-size: 12px; display: block">{{ h.note }}</n-text>
                </n-timeline-item>
              </n-timeline>
              <div v-if="historyPageCount > 1" style="display:flex; justify-content:center; margin-top: 10px">
                <n-pagination v-model:page="historyPage" :page-count="historyPageCount"
                              size="small" @update:page="loadHistory" />
              </div>
            </n-spin>
          </n-collapse-item>
        </n-collapse>

        <!-- edit mode -->
        <n-form v-else label-placement="top">
          <n-form-item v-if="isCreate" :label="t('addresses.ip')" required style="margin-bottom: 12px">
            <n-input v-model:value="createIp" placeholder="192.168.1.10" />
          </n-form-item>
          <n-space :size="12" :wrap-item="false" style="flex-wrap: wrap">
            <n-form-item :label="t('addresses.hostname')" style="flex: 1 1 300px">
              <n-input v-model:value="form.hostname" placeholder="host.example.com" />
            </n-form-item>
            <n-form-item :label="t('common.status')" style="flex: 0 0 160px">
              <n-select v-model:value="form.state" :options="stateOptions" />
            </n-form-item>
          </n-space>
          <n-form-item v-if="!isCreate" :label="t('hostnameSrc.pin_label')" style="margin-bottom: 12px">
            <n-select
              v-model:value="form.hostname_source_pin"
              :options="pinOptions" :placeholder="t('hostnameSrc.auto')"
            />
            <template #feedback>
              <span style="font-size: 11px; opacity: .7; display: block; padding-bottom: 4px">{{ t("hostnameSrc.pin_hint") }}</span>
            </template>
          </n-form-item>
          <n-space :size="12" :wrap-item="false" style="flex-wrap: wrap">
            <n-form-item :label="t('addresses.mac')" style="flex: 1 1 240px">
              <n-input v-model:value="form.mac" placeholder="aa:bb:cc:dd:ee:ff" />
            </n-form-item>
            <n-form-item :label="t('addresses.owner')" style="flex: 1 1 240px">
              <n-input v-model:value="form.owner" />
            </n-form-item>
            <!-- 交換器位置：拆成兩格。單一輸入框會讓人照著唯讀畫面打成 sw@port，
                 但實際儲存格式是「交換器 / 埠」，打錯就顯示成一整團字串 -->
            <n-form-item :label="t('addresses.switch_port')" style="flex: 1 1 320px">
              <!-- 一行到底：交換器｜@｜埠。先前用 n-space 排，欄寬不夠時會把第二個
                   輸入框擠到下一行，「@」孤零零留在右邊 —— 看起來像壞掉。
                   input-group 會自己縮，不會換行。 -->
              <n-input-group>
                <n-input v-model:value="swName" :placeholder="t('addresses.switch_name_ph')"
                         style="width: 58%" />
                <n-input-group-label style="padding: 0 8px">@</n-input-group-label>
                <n-input v-model:value="swPort" :placeholder="t('addresses.switch_port_ph')"
                         style="width: 42%" />
              </n-input-group>
              <template #feedback>
                <span style="font-size: 11.5px; opacity: .7">
                  {{ t("addresses.switch_port_hint") }}
                </span>
              </template>
            </n-form-item>
          </n-space>
          <n-form-item :label="t('common.description')">
            <n-input v-model:value="form.description" type="textarea" :rows="2" />
          </n-form-item>
          <n-form-item :label="t('addresses.note')">
            <n-input v-model:value="form.note" type="textarea" :rows="2" />
          </n-form-item>
          <n-form-item :label="t('nav.customers')">
            <n-select v-model:value="form.customer_id" :options="customerOptions"
                      :placeholder="t('common.not_specified')" clearable filterable />
          </n-form-item>
          <n-form-item :label="t('nav.devices')">
            <n-space vertical :size="4" style="width: 100%">
              <n-select v-model:value="form.device_id" :options="deviceOptions"
                        :placeholder="t('common.not_specified')" clearable filterable />
              <n-button v-if="matchingDevice" size="tiny" dashed type="primary"
                        @click="linkMatchingDevice">
                <template #icon><n-icon><LinkIcon /></n-icon></template>
                {{ t("addresses.link_matching_device", { name: matchingDevice.name }) }}
              </n-button>
              <template v-if="!form.device_id && suggestion">
                <n-button v-if="suggestion.existing_device_id" size="tiny" dashed type="primary"
                          :loading="applyingSuggestion" @click="applySuggestion(false)">
                  <template #icon><n-icon><LinkIcon /></n-icon></template>
                  {{ t("addresses.suggest_link", { name: suggestion.existing_device_name }) }}
                </n-button>
                <n-button v-else-if="suggestion.suggested_name && suggestion.can_create"
                          size="tiny" dashed type="primary"
                          :loading="applyingSuggestion" @click="applySuggestion(true)">
                  <template #icon><n-icon><PlusIcon /></n-icon></template>
                  {{ t("addresses.suggest_create", { name: suggestion.suggested_name }) }}
                </n-button>
                <template v-if="suggestion.siblings.length
                            && (suggestion.existing_device_id || suggestion.can_create)">
                  <div class="sug-note">
                    {{ t("addresses.suggest_siblings_hint", { n: suggestion.siblings.length }) }}
                  </div>
                  <n-checkbox-group v-model:value="linkIds" class="sug-siblings">
                    <n-space vertical :size="2">
                      <n-checkbox v-for="s in suggestion.siblings" :key="s.id" :value="s.id"
                                  size="small">
                        <span class="sug-ip">{{ s.ip }}</span>
                        <span class="sug-mac">{{ s.mac || "—" }}</span>
                        <span class="sug-vendor">{{ s.mac_vendor || "" }}</span>
                        <n-tag v-if="s.same_mac" size="tiny" type="success" :bordered="false">
                          {{ t("addresses.suggest_same_mac") }}
                        </n-tag>
                      </n-checkbox>
                    </n-space>
                  </n-checkbox-group>
                </template>
              </template>
            </n-space>
          </n-form-item>
          <n-form-item :label="t('scan_probes.excluded')">
            <n-space vertical :size="4" style="width: 100%">
              <n-checkbox-group v-model:value="excludedProbes">
                <n-space :size="[16, 8]" style="flex-wrap: wrap">
                  <n-checkbox v-for="p in catalog.probes" :key="p.key" :value="p.key">
                    {{ probeLabel(p, locale) }}
                    <n-tooltip v-if="p.intrusive" trigger="hover">
                      <template #trigger>
                        <n-tag size="tiny" type="warning" style="margin-left: 4px;">
                          {{ t("scan_probes.intrusive") }}
                        </n-tag>
                      </template>
                      {{ t("scan_probes.intrusive_warn") }}
                    </n-tooltip>
                  </n-checkbox>
                </n-space>
              </n-checkbox-group>
              <span style="font-size: 11px; opacity: .7">{{ t("scan_probes.excluded_hint") }}</span>
            </n-space>
          </n-form-item>
          <n-form-item :label="t('addresses.ptr_ignore')">
            <n-switch v-model:value="form.ptr_ignore" />
          </n-form-item>
          <n-form-item :label="t('ssh.enable_label')">
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.ssh_enabled" />
              <span style="font-size: 11px; opacity: .7">{{ t("ssh.enable_hint") }}</span>
            </n-space>
          </n-form-item>
          <n-form-item :label="t('sftp.enable_label')">
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.sftp_enabled" />
              <span style="font-size: 11px; opacity: .7">{{ t("sftp.enable_hint") }}</span>
            </n-space>
          </n-form-item>
          <n-form-item :label="t('rdp.enable_label')">
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.rdp_enabled" />
              <span style="font-size: 11px; opacity: .7">{{ t("rdp.enable_hint") }}</span>
            </n-space>
          </n-form-item>
          <n-form-item :label="t('vnc.enable_label')">
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.vnc_enabled" />
              <span style="font-size: 11px; opacity: .7">{{ t("vnc.enable_hint") }}</span>
            </n-space>
          </n-form-item>
          <!-- PVE 主控台開關：僅在此 IP 對應到 Proxmox VE 的 VM/CT 時出現 -->
          <n-form-item v-if="props.address?.pve">
            <template #label>
              {{ t("novnc.enable") }}
              <n-tag size="tiny" type="warning" :bordered="false" style="margin-left:6px">PVE</n-tag>
            </template>
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.novnc_enabled" />
              <span style="font-size: 11px; opacity: .7">{{ t("novnc.enable_hint") }}（{{ props.address.pve.kind === 'ct' ? 'LXC → xterm' : 'QEMU → noVNC' }} · vmid {{ props.address.pve.vmid }}）</span>
            </n-space>
          </n-form-item>
          <n-form-item>
            <template #label>
              {{ t("bmc.enable_label") }}
              <n-tag size="tiny" type="warning" :bordered="false" style="margin-left:6px">Beta</n-tag>
            </template>
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.bmc_enabled" />
              <span style="font-size: 11px; opacity: .7">{{ t("bmc.enable_hint") }}</span>
            </n-space>
          </n-form-item>
          <n-form-item :label="t('addresses.is_dhcp_server')">
            <n-space vertical :size="2" style="width:100%">
              <n-switch v-model:value="form.is_dhcp_server" />
              <span style="font-size: 11px; opacity: .7">{{ t("addresses.is_dhcp_server_hint") }}</span>
            </n-space>
          </n-form-item>
        </n-form>
      </div>

      <template v-if="!inline" #footer>
        <n-space justify="space-between">
          <n-popconfirm v-if="!isCreate && (!inline || editMode)" key="ft-del" @positive-click="remove">
            <template #trigger>
              <n-button type="error" ghost size="small" :loading="deleting" :disabled="!props.address">
                <template #icon><n-icon><DeleteIcon /></n-icon></template>
                {{ t("common.delete") }}
              </n-button>
            </template>
            {{ t("common.confirm_delete") }}
          </n-popconfirm>
          <span v-else></span>
          <n-space>
            <n-button v-if="!inline || editMode || isCreate" key="ft-cancel" @click="close">
              <template #icon><n-icon><CancelIcon /></n-icon></template>
              {{ t("common.cancel") }}
            </n-button>
            <n-button v-if="isCreate" key="ft-create" type="primary" :loading="saving" @click="save">
              <template #icon><n-icon><PlusIcon /></n-icon></template>
              {{ t("common.create") }}
            </n-button>
            <n-button v-else-if="!editMode" key="ft-edit" type="primary" @click="editMode = true">
              <template #icon><n-icon><EditIcon /></n-icon></template>
              {{ t("common.edit") }}
            </n-button>
            <n-button v-else key="ft-save" type="success" :loading="saving" @click="save">
              <template #icon><n-icon><SaveIcon /></n-icon></template>
              {{ t("common.save") }}
            </n-button>
          </n-space>
        </n-space>
      </template>
    </n-card>
  </component>
  <InvestigateModal v-if="props.address?.ip"
                    v-model:show="investigating" :ip="String(props.address.ip)" />
</template>

<style scoped>
.nat-ref {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 6px;
  background: rgba(127, 127, 127, 0.06); cursor: pointer;
  transition: background .15s;
}
.nat-ref:hover { background: rgba(24, 160, 88, 0.12); }
.nat-ref-name { font-weight: 500; }
.nat-ref-meta { font-size: 12px; opacity: 0.6; font-family: monospace; }
/* RDP/VNC Beta 角落小標：疊在按鈕右上角，不佔橫向空間 */
.conn-beta-wrap { position: relative; display: inline-flex; }
.conn-beta-badge {
  position: absolute; top: -7px; right: -6px; z-index: 2; pointer-events: none;
  font-size: 9px; font-weight: 700; line-height: 1; letter-spacing: .2px;
  padding: 1px 4px; border-radius: 999px;
  color: #fff; background: #d99812; box-shadow: 0 0 0 1.5px var(--n-color, #fff);
}
.conn-sol-badge { background: #909399; }
/* 異動記錄超過 N 天（系統設定）的項目以淡色顯示 */
.log-dim { opacity: .45; }

/* ARP 時間旁的提醒標記：這個時間不代表機器當下活著 */
.arp-caveat {
  display: inline-block; margin-left: 6px; width: 14px; height: 14px; line-height: 14px;
  text-align: center; border-radius: 50%; font-size: 10px; cursor: help;
  background: rgba(251, 191, 36, .18); color: #b45309;
}
</style>
