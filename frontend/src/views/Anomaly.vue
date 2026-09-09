<script setup lang="ts">
import { computed, onMounted, ref, h } from "vue";
import { fmtDateTime } from "@/utils/datetime";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";
import { useEntityLinks } from "@/composables/useEntityLinks";
import {
  NCard, NSpace, NIcon, NButton, NAlert, NGrid, NGi, NDataTable, NEmpty,
  NTabs, NTabPane, NModal, NSelect, NSwitch, NInputNumber, NTimePicker,
  useMessage, type DataTableColumns,
} from "naive-ui";
import {
  getAnomalySchedule, ignoreAnomalyForIp, listIgnorableCategories,
  runAnomalyScan, updateAnomalySchedule,
  type AnomalyReport, type AnomalySchedule,
} from "@/api/phase3";
import { AiAuditIcon, AnomalyIcon, DownloadIcon, EyeIcon, InfoIcon, PendingIcon, SettingsIcon, TestIcon, renderIcon } from "@/icons";
import { renderMarkdown } from "@/utils/markdown";
import { downloadTextFile } from "@/utils/investigateReport";
import { listSubnets, setAnomalyScope } from "@/api/subnets";
import { apiClient, apiErrMsg } from "@/api/client";
import { autoSort } from "@/composables/useTableSort";
import type { Subnet } from "@/types";
import { useTablePagination } from "@/composables/useTablePagination";
import { useColumnPrefs } from "@/composables/useColumnPrefs";
import ColumnPicker from "@/components/ColumnPicker.vue";

const { t, te } = useI18n();
const msg = useMessage();
const pg = useTablePagination();
const loading = ref(false);
const report = ref<AnomalyReport | null>(null);
const lastRunAt = ref<string | null>(null);

// 偵測範圍（寫的是 subnets.anomaly_enabled，跟子網路編輯頁同一個欄位）
const scopeShow = ref(false);
const scopeSaving = ref(false);
const scopeIds = ref<string[]>([]);
const subnets = ref<Subnet[]>([]);
const subnetsLoading = ref(false);
const subnetOptions = computed(() => subnets.value.map((s) => ({
  label: s.description ? `${s.cidr} — ${s.description}` : s.cidr,
  value: s.id,
})));

async function loadSubnets() {
  subnetsLoading.value = true;
  try {
    const r = await listSubnets({ pageSize: 500 });
    subnets.value = r.items;
    scopeIds.value = r.items.filter((s) => s.anomaly_enabled).map((s) => s.id);
  } catch { /* 沒權限就留空，不擋整頁 */ } finally { subnetsLoading.value = false; }
}

function openScope() {
  scopeShow.value = true;
  void loadSubnets();
}

// ── 排程 ─────────────────────────────────────────────────────────────────
// 偵測邏輯早就寫好了，但只能靠人按「執行掃描」—— IP 衝突、非法 DHCP 不會挑上班時間發生。
// 形狀刻意與 AI 巡檢排程一致（同一份 due() 判斷），另外多一個「每隔 N 分鐘」：
// 那些是營運監控，等到隔天太慢。
const schedShow = ref(false);
const schedSaving = ref(false);
const sched = ref<AnomalySchedule | null>(null);

const freqOptions = computed(() => [
  { label: t("anomaly.sched_freq_interval"), value: "interval" },
  { label: t("anomaly.sched_freq_daily"), value: "daily" },
  { label: t("anomaly.sched_freq_weekly"), value: "weekly" },
  { label: t("anomaly.sched_freq_monthly"), value: "monthly" },
]);
// 星期的文案沿用巡檢排程那一份（`llm_settings.weekday_N`）—— 另造一套鍵值
// 只會多出一份要維護的翻譯，而且漏翻時畫面上會直接露出鍵名。
const weekdayOptions = computed(() => [1, 2, 3, 4, 5, 6, 7].map((d) => ({
  label: t(`llm_settings.weekday_${d}`), value: d,
})));

function hhmmToMs(hhmm: string): number {
  const [h, m] = hhmm.split(":").map((x) => Number(x) || 0);
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.getTime();
}

async function openSched() {
  try {
    sched.value = await getAnomalySchedule();
    schedShow.value = true;
  } catch (e) { msg.error(apiErrMsg(e)); }
}

async function patchSched(patch: Partial<AnomalySchedule>) {
  if (!sched.value) return;
  schedSaving.value = true;
  try {
    sched.value = await updateAnomalySchedule(patch);
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { schedSaving.value = false; }
}

function setSchedTime(index: number, formatted: string | null) {
  if (!formatted || !sched.value) return;
  const next = [...sched.value.times];
  next[index] = formatted;
  void patchSched({ times: next });
}

function removeSchedTime(index: number) {
  if (!sched.value) return;
  const next = sched.value.times.filter((_, i) => i !== index);
  // 一個都不留＝排程開著卻永遠不觸發。後端也會擋，這裡先不讓它發生
  if (next.length) void patchSched({ times: next });
}

async function saveScope() {
  scopeSaving.value = true;
  try {
    await setAnomalyScope(scopeIds.value);
    msg.success(t("common.saved"));
    scopeShow.value = false;
  } catch (e) { msg.error(apiErrMsg(e)); await loadSubnets(); }
  finally { scopeSaving.value = false; }
}
// 可以逐 IP 忽略的類別。清單由後端決定（`/anomalies/ignorable`），這裡的預設值只是
// 在還沒載回來之前不要讓按鈕閃現；**不要**在前端另外寫死一份 —— 兩份清單遲早會不一致。
const IGNORABLE = ref<string[]>([]);
const ignoreBusy = ref<Set<string>>(new Set());

async function loadIgnorable() {
  try { IGNORABLE.value = (await listIgnorableCategories()).categories; } catch { /* 靜默 */ }
}

async function doIgnore(ipId: string, category: string) {
  ignoreBusy.value = new Set([...ignoreBusy.value, ipId]);
  try {
    await ignoreAnomalyForIp(ipId, category);
    msg.success(t("anomaly.ignore_done"));
    await run();                      // 重跑一次，被忽略的那列就會消失
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally {
    const next = new Set(ignoreBusy.value); next.delete(ipId); ignoreBusy.value = next;
  }
}

const CATEGORY_KEYS = [
  "ip_conflicts", "mac_drifts", "ghost_ips", "unauthorized_ips", "rogue_dhcp",
  "external_exposure", "dangling_dns", "duplicate_ip_records", "suspicious_changes",
  "fw_rule_rot",
  "arp_only_liveness",
  "stale_device_links",
  "mac_flapping",
];
const route = useRoute();
const links = useEntityLinks(useRouter());
// 通知點進來要落在對應的頁籤（?tab=fw_rule_rot），不是丟到第一個分類讓人自己找
const activeTab = ref(
  CATEGORY_KEYS.includes(String(route.query.tab)) ? String(route.query.tab) : "ip_conflicts");

type CatKey = "ip_conflicts" | "mac_drifts" | "ghost_ips" | "unauthorized_ips"
  | "rogue_dhcp" | "external_exposure" | "dangling_dns" | "duplicate_ip_records" | "suspicious_changes"
  | "fw_rule_rot"
  | "arp_only_liveness"
  | "stale_device_links"
  | "mac_flapping";
const CATEGORIES: { key: CatKey; label: () => string }[] = [
  { key: "ip_conflicts", label: () => t("anomaly.ip_conflicts") },
  { key: "mac_drifts", label: () => t("anomaly.mac_drifts") },
  { key: "ghost_ips", label: () => t("anomaly.ghost_ips") },
  { key: "unauthorized_ips", label: () => t("anomaly.unauthorized") },
  { key: "rogue_dhcp", label: () => t("anomaly.rogue_dhcp") },
  { key: "external_exposure", label: () => t("anomaly.exposure") },
  { key: "dangling_dns", label: () => t("anomaly.dangling_dns") },
  { key: "duplicate_ip_records", label: () => t("anomaly.dup_ip") },
  { key: "suspicious_changes", label: () => t("anomaly.changes") },
  { key: "fw_rule_rot", label: () => t("anomaly.fw_rot") },
  { key: "arp_only_liveness", label: () => t("anomaly.arp_only") },
  { key: "stale_device_links", label: () => t("anomaly.stale_link") },
  { key: "mac_flapping", label: () => t("anomaly.mac_flapping") },
];

const rogueTitle = computed(() =>
  t("anomaly.rogue_dhcp") + `（${report.value?.rogue_dhcp?.length ?? 0}）`);

// 首屏四張統計卡；數字 > 0 用警示色，一眼看得出哪一類有事
const statCards = computed(() => {
  const r = report.value;
  if (!r) return [];
  return [
    { key: "ip_conflicts", label: t("anomaly.ip_conflicts"), value: r.ip_conflicts.length },
    { key: "mac_drifts", label: t("anomaly.mac_drifts"), value: r.mac_drifts.length },
    { key: "ghost_ips", label: t("anomaly.ghost_ips"), value: r.ghost_ips.length },
    { key: "unauthorized_ips", label: t("anomaly.unauthorized"),
      value: r.unauthorized_ips.length },
  ];
});

const anyFindings = computed(() => {
  const r = report.value;
  return !!r && (r.ip_conflicts.length + r.mac_drifts.length + r.ghost_ips.length
    + r.unauthorized_ips.length + (r.rogue_dhcp?.length ?? 0)
    + (r.external_exposure?.length ?? 0) + (r.dangling_dns?.length ?? 0)
    + (r.duplicate_ip_records?.length ?? 0) + (r.suspicious_changes?.length ?? 0)
    + (r.fw_rule_rot?.length ?? 0)
    + (r.arp_only_liveness?.length ?? 0)
    + (r.stale_device_links?.length ?? 0)
    + (r.mac_flapping?.length ?? 0)) > 0;
});
function catRows(key: CatKey): Record<string, any>[] {
  return (report.value?.[key] as Record<string, any>[]) ?? [];
}

// 欄位標題（技術欄名在地化；其餘原樣）
const COLLBL: Record<string, string> = {
  mac: "MAC", macs: "MAC", ip: "IP", ips: "對應 IP / 主機名稱", hostname: "主機名稱",
  port: "埠", device_id: "裝置", last_seen_at: "最後出現", locations: "出現位置",
  last_seen_scanner: "最後出現（掃描）", last_seen_librenms: "最後出現（LibreNMS）",
  last_seen_arp: "最後出現（ARP）",
  ip_address_id: "IP 物件 ID", reason: "原因", subnet: "子網路", state: "狀態",
  server_ip: "DHCP 伺服器 IP", subnet_cidr: "子網路", vendor: "廠商",
  offered_ip: "發出的 IP", router: "指定的閘道", first_seen_at: "首次發現",
  kind: "狀況", ports: "對外開放的埠", monitored: "監控涵蓋",
  name: "名稱", value: "指向", type: "型別", zone: "區域", server: "DNS 伺服器",
  records: "重複的紀錄", actor: "操作者", actor_ip: "來源 IP",
  action: "動作", count: "次數", first_at: "最早", object_type: "物件類型",
  effective_status: "存活狀態", names: "DNS 名稱", owner: "負責人", rules: "來源規則",
  source: "來源", interface: "介面", descr: "規則描述", detail: "說明",
  ip_id: "IP 內部編號",
  days: "統計天數",
  randomized: "隨機化位址",
  mac_count: "MAC 數",
};
// 各類別的欄位（順序）＋預設隱藏（ip_address_id 是內部 UUID，預設不顯示，可在「欄位」勾選）
const CAT_KEYS: Record<CatKey, string[]> = {
  ip_conflicts: ["ip", "macs"],
  mac_drifts: ["mac", "ips", "locations"],
  ghost_ips: ["ip", "hostname", "last_seen_scanner", "last_seen_librenms", "ip_address_id"],
  unauthorized_ips: ["ip"],
  rogue_dhcp: ["server_ip", "subnet_cidr", "mac", "vendor", "offered_ip", "router",
               "first_seen_at", "last_seen_at"],
  external_exposure: ["kind", "ip", "hostname", "ports", "subnet", "monitored",
                      "effective_status", "names", "owner", "rules", "ip_address_id"],
  dangling_dns: ["name", "value", "type", "zone", "server"],
  duplicate_ip_records: ["ip", "records"],
  suspicious_changes: ["kind", "actor", "actor_ip", "object_type", "action",
                       "count", "first_at", "last_at"],
  fw_rule_rot: ["kind", "name", "source", "interface", "port", "descr", "detail"],
  arp_only_liveness: ["ip", "hostname", "mac", "last_seen_arp", "ip_address_id"],
  // 頻繁換 MAC：先看是哪個 IP、換過幾個、時間跨度，再看 MAC 清單。
  // randomized 要露出來 —— 那一欄是「這些看起來是隱私隨機化位址」，
  // 使用者據此判斷要不要把這個 IP 加進忽略清單。
  mac_flapping: ["ip", "hostname", "mac_count", "randomized", "days", "macs", "ip_id"],
  stale_device_links: ["ip", "hostname", "mac", "device", "linked_at", "mac_changed_at", "ip_address_id"],
};
const CAT_HIDDEN: Partial<Record<CatKey, string[]>> = {
  mac_flapping: ["ip_id", "days"],
  ghost_ips: ["ip_address_id"],
  // owner 實務上幾乎沒人填、rules 是原始規則明細、ip_address_id 是內部 UUID：
  // 預設不顯示，需要的人可在「欄位」自行勾選
  external_exposure: ["ip_address_id", "owner", "rules"],
};

// 每個類別一份欄位顯示偏好
const prefs = {} as Record<CatKey, ReturnType<typeof useColumnPrefs>>;
for (const c of CATEGORIES) {
  const keys = CAT_KEYS[c.key];
  const hidden = CAT_HIDDEN[c.key] ?? [];
  prefs[c.key] = useColumnPrefs(`anomaly_${c.key}`, keys, keys.filter((k) => !hidden.includes(k)));
}
function pickerItems(key: CatKey) {
  return CAT_KEYS[key].map((k) => ({ key: k, label: COLLBL[k] ?? k }));
}

function pretty(k: string, val: any): string {
  if (val == null || val === "") return "";
  // kind 是分類代碼（exposed_unmonitored…），要翻成看得懂的字，不能把 enum 直接印給人看
  // kind 橫跨兩類（對外曝險 exp_* 與可疑變更 chg_*），找得到才翻，找不到就原樣顯示
  if (k === "kind") {
    for (const p of ["anomaly.exp_", "anomaly.chg_"]) {
      const key = `${p}${val}`;
      if (te(key)) return t(key);
    }
    return String(val);
  }
  if (k === "monitored" || k === "randomized") return val ? t("common.yes") : t("common.no");
  if (Array.isArray(val)) {
    return val.map((x) => (typeof x === "object" && x !== null ? objLine(x) : String(x)))
      .join("、");
  }
  if (k.includes("device_id")) return String(val).slice(0, 8);
  if (k.includes("last_seen") || k.includes("_at") || k.includes("time")) return fmtDateTime(String(val));   // 轉本地時區
  return String(val);
}
function objLine(o: Record<string, any>): string {
  return Object.entries(o)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `${COLLBL[k] ?? k}：${pretty(k, v)}`)
    .join("　·　");
}
function cell(label: string, val: string) {
  return h("span", { style: "white-space:nowrap;overflow:hidden;text-overflow:ellipsis" }, [
    h("span", { style: "opacity:.55;margin-right:4px" }, label),
    h("span", val || "—"),
  ]);
}
function renderLocation(o: Record<string, any>) {
  const dev = o.device_name || (o.device_id ? String(o.device_id).slice(0, 8) : "—");
  return h("div", {
    style: "display:grid;grid-template-columns:minmax(0,1fr) 110px 132px;gap:14px;font-size:12.5px;align-items:baseline",
  }, [
    cell(COLLBL.device_id, dev),
    cell(COLLBL.port, o.port ?? "—"),
    cell(COLLBL.last_seen_at, pretty("last_seen_at", o.last_seen_at)),
  ]);
}
function renderMac(o: Record<string, any>) {
  // 本地管理位址（虛擬機／容器／手機 MAC 隨機化）沒有 OUI 登記，查不到廠商是正常的。
  // 標出來才看得懂：同一 IP 上「真實 MAC + 隨機 MAC」多半是同一台裝置，不是兩台在搶。
  const tag = o.local
    ? h("span", { class: "mac-tag mac-tag--local" }, t("anomaly.mac_local"))
    : (o.vendor ? h("span", { class: "mac-tag" }, String(o.vendor)) : null);
  return h("div", { style: "display:flex;align-items:baseline;gap:8px;font-size:12.5px" }, [
    h("span", { style: "font-family:var(--jt-mono,monospace)" }, o.mac ?? "—"),
    tag,
    h("span", { style: "opacity:.55;margin-left:auto;white-space:nowrap" },
      pretty("last_seen_at", o.last_seen_at)),
  ]);
}
// 表格裡的 IP 要能點進 IP 詳細資料（回報：看到可疑 IP 卻只能自己複製去搜）。
// 有 ip_address_id 就直接開那一筆；只有文字就帶去 /addresses?q=<ip> 搜尋。
function renderIp(row: any, ipText: string) {
  return row?.ip_address_id
    ? links.ipById(row.ip_address_id, ipText)
    : links.ipByText(ipText);
}
function renderVal(k: string, v: any, row?: any) {
  if (v == null || v === "") return "—";
  if ((k === "ip" || k === "server_ip" || k === "offered_ip") && typeof v === "string") {
    return renderIp(row, v);
  }
  // MAC 歷程：一個 MAC 一行、**不折行**。MAC 字串被折成「0a:1b:2 / c:00:00 / :01」
  // 三行的話，這一欄就完全讀不出先後順序 —— 而先後順序正是這一類要看的東西。
  if (k === "macs" && Array.isArray(v) && v.length && typeof v[0] === "object") {
    return h("div", { style: "display:flex;flex-direction:column;gap:2px;font-size:12.5px" },
      v.map((it: any) => h("div", { style: "white-space:nowrap" }, [
        h("span", { style: "font-family:var(--mono,monospace)" }, String(it.mac ?? "")),
        it.last_seen_at
          ? h("span", { style: "opacity:.65;margin-left:8px" }, fmtDateTime(String(it.last_seen_at)))
          : null,
      ])));
  }
  if (k === "ips" && Array.isArray(v)) {
    if (!v.length) return h("span", { style: "opacity:.5" }, "—");
    return h("div", { style: "display:flex;flex-direction:column;gap:2px;font-size:12.5px" },
      v.map((it: any) => h("div", null, [
        it.ip_address_id ? links.ipById(it.ip_address_id, it.ip) : links.ipByText(it.ip),
        it.hostname ? h("span", { style: "opacity:.7" }, `（${it.hostname}）`) : null,
      ])));
  }
  if (k === "macs" && Array.isArray(v)) {
    return h("div", { style: "display:flex;flex-direction:column;gap:3px" }, v.map(renderMac));
  }
  if (Array.isArray(v)) {
    return h("div", { style: "display:flex;flex-direction:column;gap:3px" },
      v.map((it: any) => k === "locations"
        ? renderLocation(it)
        : h("div", { style: "font-size:12.5px" }, it && typeof it === "object" ? objLine(it) : String(it))));
  }
  if (typeof v === "object") return objLine(v);
  return pretty(k, v);
}
// 依該類別的可見欄位（已套欄位偏好）組欄位
function catCols(key: CatKey): DataTableColumns<any> {
  const visible = prefs[key].visibleKeys.value;
  // autoSort：與全站表格一致，替沒有自訂 sorter 的欄位補上預設排序。
  // 這幾張表原本整排標頭都不能排 —— 十幾筆 MAC 變動想按時間或按網段看都做不到。
  const cols = autoSort(CAT_KEYS[key].filter((k) => visible.includes(k)).map((k) => {
    // MAC 清單一列要放好幾個「MAC＋時間」，窄欄會擠成一團看不出先後
    const wide = k === "locations" || k === "macs";
    return {
      title: COLLBL[k] ?? k,
      key: k,
      minWidth: wide ? 420 : (k === "ips" ? 220 : 140),
      // MAC 歷程不折行 —— 沒有明確寬度時會蓋到「操作」欄的按鈕上
      ...(k === "macs" ? { width: 340 } : {}),
      ellipsis: wide || k === "ips" ? false : { tooltip: true },
      render: (r: any) => renderVal(k, r[k], r),
    };
  }));
  // 可以逐 IP 忽略的類別：給一顆「忽略這個 IP」。
  // 沒有這個機制的話，開了隱私隨機化的裝置（Windows 11／macOS／iOS／Android，每次
  // 連線都換 MAC）會把整頁洗掉，使用者只能把整條規則關掉 —— 連真正的 IP 搶用也一起看不到。
  if (IGNORABLE.value.includes(key)) {
    cols.push({
      title: t("common.actions"), key: "_ignore", width: 150, className: "col-actions",
      render: (r: any) => (r.ip_id
        ? h(NButton, {
            size: "tiny", secondary: true, loading: ignoreBusy.value.has(r.ip_id),
            disabled: ignoreBusy.value.has(r.ip_id),
            onClick: () => doIgnore(r.ip_id, key),
          }, { default: () => t("anomaly.ignore_btn") })
        : null),
    } as any);
  }

  // 未授權 IP：加「AI 判讀」—— 把「有一個不明 IP」變成「看起來是什麼、下一步查哪」。
  // 欄位標題用「操作」——與按鈕同名看起來像重複貼兩次（使用者回饋，與規則異動頁同一批）。
  if (key === "unauthorized_ips") {
    cols.push({
      title: t("common.actions"), key: "_triage", width: 200, className: "col-actions",
      render: (r: any) => h("span",
        { style: "display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap" }, [
          h(NButton, {
            size: "tiny", secondary: true, loading: triageBusy.value.has(r.ip),
            disabled: triageBusy.value.has(r.ip),
            onClick: () => doTriage(r.ip),
          }, { icon: renderIcon(AiAuditIcon, 15), default: () => t("anomaly.triage_btn") }),
          triageResults.value[r.ip]
            ? h(NButton, { size: "tiny", type: "primary", secondary: true,
                           onClick: () => { triageShow.value = r.ip; } },
                { icon: renderIcon(EyeIcon, 15), default: () => t("fw_changes.ai_view") })
            : null,
        ]),
    } as any);
  }
  return cols;
}

// AI 判讀：LLM 要跑幾十秒 —— 背景執行，完成後該列長出「檢視結果」，
// 結果留在頁面可重看（與防火牆規則異動的 AI 解讀同一套體驗）。
interface TriageResult { ip: string; card: string; disclaimer: string; model?: string }
const triageBusy = ref<Set<string>>(new Set());
const triageResults = ref<Record<string, TriageResult>>({});
const triageShow = ref<string | null>(null);
async function doTriage(ip: string) {
  triageBusy.value.add(ip); triageBusy.value = new Set(triageBusy.value);
  try {
    const { data } = await apiClient.post("/api/v1/anomalies/triage", { ip });
    triageResults.value = { ...triageResults.value, [ip]: data };
    msg.success(t("fw_changes.ai_done"));
  } catch (e: any) {
    msg.error(e?.response?.data?.detail ?? t("errors.server"));
  } finally { triageBusy.value.delete(ip); triageBusy.value = new Set(triageBusy.value); }
}

/** 下載判讀報告：.md 保留原始 markdown；.txt 去標記純文字（與規則異動頁一致）。 */
function downloadTriage(fmt: "md" | "txt") {
  const ip = triageShow.value;
  const res = ip ? triageResults.value[ip] : null;
  if (!ip || !res) return;
  const header = [
    `# ${t("anomaly.triage_title", { ip })}`,
    "",
    `- ${t("fw_changes.ai_model")}：${res.model ?? "—"}`,
    "",
    `> ${res.disclaimer}`,
    "",
  ].join("\n");
  const body = header + res.card + "\n";
  const text = fmt === "md" ? body : body
    .replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1")
    .replace(/^#{1,6}\s+/gm, "").replace(/^>\s?/gm, "");
  downloadTextFile(text, `ip-triage-${ip}.${fmt}`, fmt);
}

async function run() {
  loading.value = true;
  try {
    report.value = await runAnomalyScan();
    lastRunAt.value = fmtDateTime(new Date());
  } catch (e: any) {
    msg.error(e?.response?.data?.detail ?? t("errors.server"));
  } finally {
    loading.value = false;
  }
}
onMounted(() => { void loadIgnorable(); });
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><AnomalyIcon /></n-icon>
        <span>{{ t("anomaly.title") }}</span>
      </n-space>
    </template>
    <n-space align="center" style="margin-bottom: 12px" :wrap-item="false">
      <n-button type="primary" :loading="loading" @click="run">
        <template #icon><n-icon><TestIcon /></n-icon></template>
        {{ t("anomaly.run_scan") }}
      </n-button>
      <span v-if="lastRunAt" style="opacity: 0.7; font-size: 13px">
        {{ t("anomaly.last_run") }}: {{ lastRunAt }}
      </span>
      <n-button size="small" quaternary @click="openScope">
        <template #icon><n-icon><SettingsIcon /></n-icon></template>
        {{ t("anomaly.scope_btn") }}
      </n-button>
      <n-button size="small" quaternary @click="openSched">
        <template #icon><n-icon><PendingIcon /></n-icon></template>
        {{ t("anomaly.sched_btn") }}
      </n-button>
    </n-space>

    <!-- 偵測範圍：訪客／實驗網段本來就會一堆異常，留著只會把真正該處理的埋掉 -->
    <n-modal v-model:show="scopeShow" preset="card" style="max-width: 620px"
             :title="t('anomaly.scope_title')">
      <n-alert type="info" :bordered="false" style="margin-bottom:12px">
        {{ t("anomaly.scope_hint") }}
      </n-alert>
      <n-select v-model:value="scopeIds" multiple filterable clearable
                :options="subnetOptions" :loading="subnetsLoading"
                :placeholder="t('anomaly.scope_none')" />
      <template #footer>
        <n-space justify="end">
          <n-button @click="scopeShow = false">{{ t("common.cancel") }}</n-button>
          <n-button type="primary" :loading="scopeSaving" @click="saveScope">
            {{ t("common.save") }}
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 排程：跑的是同一支偵測，差別在通知只發「與上次相比是新的」 -->
    <n-modal v-model:show="schedShow" preset="card" style="max-width: 640px"
             :title="t('anomaly.sched_title')">
      <n-alert type="info" :bordered="false" style="margin-bottom:12px">
        {{ t("anomaly.sched_hint") }}
      </n-alert>
      <n-space v-if="sched" vertical :size="14">
        <div>
          <n-switch :value="sched.schedule_enabled" :loading="schedSaving"
                    @update:value="(v: boolean) => patchSched({ schedule_enabled: v })" />
          <span style="margin-left:10px">{{ t("anomaly.sched_enabled") }}</span>
          <p class="sched-hint">{{ t("anomaly.sched_enabled_hint") }}</p>
        </div>
        <div>
          <label>{{ t("anomaly.sched_freq") }}</label>
          <n-space align="center" :size="12" :wrap="true">
            <n-select :value="sched.frequency" :options="freqOptions" style="width: 170px"
                      :disabled="!sched.schedule_enabled"
                      @update:value="(v: AnomalySchedule['frequency']) => patchSched({ frequency: v })" />
            <n-input-number v-if="sched.frequency === 'interval'" :value="sched.interval_minutes"
                            :min="5" :max="1440" :step="5" style="width: 150px"
                            :disabled="!sched.schedule_enabled"
                            @update:value="(v: number | null) => v && patchSched({ interval_minutes: v })" />
            <n-select v-if="sched.frequency === 'weekly'" :value="sched.weekdays" multiple
                      :options="weekdayOptions" style="min-width: 260px"
                      :disabled="!sched.schedule_enabled"
                      :placeholder="t('anomaly.sched_weekdays_ph')"
                      @update:value="(v: number[]) => v.length && patchSched({ weekdays: v })" />
            <n-input-number v-if="sched.frequency === 'monthly'" :value="sched.month_day"
                            :min="1" :max="31" style="width: 130px"
                            :disabled="!sched.schedule_enabled"
                            @update:value="(v: number | null) => v && patchSched({ month_day: v })" />
          </n-space>
          <p v-if="sched.frequency === 'interval'" class="sched-hint">
            {{ t("anomaly.sched_interval_hint") }}
          </p>
          <p v-if="sched.frequency === 'monthly'" class="sched-hint">
            {{ t("anomaly.sched_month_day_hint") }}
          </p>
        </div>
        <div v-if="sched.frequency !== 'interval'">
          <label>{{ t("anomaly.sched_times") }}</label>
          <div class="sched-times">
            <div v-for="(tm, i) in sched.times" :key="i" class="sched-time-row">
              <n-time-picker :value="hhmmToMs(tm)" format="HH:mm" style="width: 130px"
                             :disabled="!sched.schedule_enabled"
                             @update:formatted-value="(v: string | null) => setSchedTime(i, v)" />
              <n-button quaternary size="small"
                        :disabled="!sched.schedule_enabled || sched.times.length <= 1"
                        @click="removeSchedTime(i)">✕</n-button>
            </div>
            <n-button size="small" quaternary :disabled="!sched.schedule_enabled"
                      @click="patchSched({ times: [...sched.times, '12:00'] })">
              + {{ t("anomaly.sched_add_time") }}
            </n-button>
          </div>
          <p class="sched-hint">{{ t("anomaly.sched_times_hint") }}</p>
        </div>
        <div style="opacity:.75; font-size:13px">
          {{ t("anomaly.sched_last_run") }}:
          {{ sched.last_run_at ? fmtDateTime(sched.last_run_at) : t("anomaly.sched_never") }}
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="schedShow = false">{{ t("common.close") }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-alert v-if="!report" type="info">
      <template #icon><n-icon><InfoIcon /></n-icon></template>
      {{ t("anomaly.help") }}
    </n-alert>

    <template v-if="report">
      <!-- 非法 DHCP 一出現幾乎必定有事：它會把錯的位址跟閘道發給整個網段的機器。
           混在分頁裡跟其它異常一樣大小的話，看到的人不會知道這條要優先處理。 -->
      <n-alert v-if="report.rogue_dhcp?.length" type="error" :show-icon="true"
               style="margin-bottom: 16px" :title="rogueTitle">
        {{ t("anomaly.rogue_dhcp_warn") }}
        <div class="rogue-list">
          <span v-for="r in report.rogue_dhcp.slice(0, 8)" :key="r.server_ip" class="rogue-chip">
            {{ r.server_ip }}<template v-if="r.subnet_cidr"> · {{ r.subnet_cidr }}</template>
            <template v-if="r.vendor"> · {{ r.vendor }}</template>
          </span>
        </div>
      </n-alert>

      <!-- 統計卡：有框有底色才看得出是一組數字，且點下去直接切到那一類（回報） -->
      <n-grid :cols="4" x-gap="12" y-gap="12" style="margin-bottom: 16px">
        <n-gi v-for="s in statCards" :key="s.key">
          <div class="anom-stat" :class="{ 'anom-stat--hit': s.value > 0 }"
               role="button" tabindex="0"
               @click="activeTab = s.key" @keyup.enter="activeTab = s.key">
            <div class="anom-stat__label">{{ s.label }}</div>
            <div class="anom-stat__value">{{ s.value }}</div>
          </div>
        </n-gi>
      </n-grid>
      <n-empty v-if="!anyFindings" :description="t('anomaly.none_found')" style="margin: 24px 0" />

      <n-tabs v-else v-model:value="activeTab" type="line" animated>
        <n-tab-pane v-for="c in CATEGORIES" :key="c.key" :name="c.key"
                    :tab="`${c.label()} (${catRows(c.key).length})`">
          <!-- 每個類別先講清楚「這是什麼、為什麼會出現」，否則一長串 IP 沒人看得懂 -->
          <n-alert type="default" :bordered="false" :show-icon="false" class="cat-note">
            {{ t(`anomaly.explain_${c.key}`) }}
          </n-alert>
          <template v-if="catRows(c.key).length">
            <div style="display:flex;justify-content:flex-end;margin-bottom:8px">
              <ColumnPicker :all="pickerItems(c.key)" :visible="prefs[c.key].visibleKeys.value"
                            @update:visible="prefs[c.key].setVisible" @reset="prefs[c.key].reset" />
            </div>
            <n-data-table :columns="catCols(c.key)" :data="catRows(c.key)"
                          :bordered="false" size="small" :scroll-x="600" :pagination="pg" />
          </template>
          <n-empty v-else :description="t('anomaly.none_found')" style="margin: 16px 0" />
        </n-tab-pane>
      </n-tabs>
    </template>
  </n-card>

  <!-- AI 鑑識卡：走全站共用 markdown 渲染器（先跳脫再產標籤，無注入面）——
       原本 pre-wrap 純文字會把 **粗體** 的星號原樣露出（使用者截圖）；＋模型標示＋下載 -->
  <n-modal :show="!!triageShow" preset="card" style="width: 560px; max-width: 94vw"
           :title="t('anomaly.triage_title', { ip: triageShow ?? '' })"
           @update:show="(v: boolean) => { if (!v) triageShow = null; }">
    <n-alert type="warning" :bordered="false" style="margin-bottom: 10px">
      {{ triageShow ? triageResults[triageShow]?.disclaimer : "" }}
    </n-alert>
    <!-- eslint-disable-next-line vue/no-v-html -->
    <div class="triage-body" v-html="renderMarkdown(triageShow ? triageResults[triageShow]?.card ?? '' : '')" />
    <template #footer>
      <div class="triage-foot">
        <span class="triage-model">{{ t("fw_changes.ai_model") }}：{{
          (triageShow ? triageResults[triageShow]?.model : "") || "—" }}</span>
        <n-space :size="8">
          <n-button size="small" secondary @click="downloadTriage('md')">
            <template #icon><n-icon><DownloadIcon /></n-icon></template>
            {{ t("fw_changes.ai_dl_md") }}
          </n-button>
          <n-button size="small" secondary @click="downloadTriage('txt')">
            <template #icon><n-icon><DownloadIcon /></n-icon></template>
            {{ t("fw_changes.ai_dl_txt") }}
          </n-button>
        </n-space>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
/* 排程視窗：提示文字要小一號、灰一點，否則整個視窗看起來都是同等重要的字 */
.sched-hint { margin: 6px 0 0; font-size: 12px; opacity: .7; line-height: 1.5; }
.sched-times { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.sched-time-row { display: flex; align-items: center; gap: 2px; }

/* 統計卡：外框 + 底色，數字有值時轉為警示色（原本是裸數字，看起來像沒對齊的散字） */
.anom-stat {
  border: 1px solid var(--n-border-color, rgba(128, 128, 128, 0.28));
  border-radius: 10px;
  padding: 10px 14px;
  background: rgba(127, 127, 127, 0.04);
  cursor: pointer;
  transition: border-color .15s ease, background .15s ease, transform .1s ease;
}
.anom-stat:hover { border-color: #18a058; transform: translateY(-1px); }
.anom-stat__label { font-size: 12.5px; opacity: .7; }
.anom-stat__value { font-size: 26px; font-weight: 600; line-height: 1.25; }
.anom-stat--hit {
  border-color: rgba(240, 160, 32, .55);
  background: rgba(240, 160, 32, .09);
}
.anom-stat--hit .anom-stat__value { color: #d97706; }

.triage-body { font-size: 13px; line-height: 1.85; }
.triage-body :deep(code) { background: rgba(128, 128, 128, .14); border-radius: 4px;
  padding: 1px 5px; font-size: 12px; }
.triage-body :deep(p) { margin: 6px 0; }
.triage-body :deep(ul), .triage-body :deep(ol) { margin: 4px 0; padding-left: 20px; }
.triage-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.triage-model { font-size: 12px; opacity: .65; }
.cat-note { margin: 4px 0 14px; font-size: 12.5px; line-height: 1.7; }
.rogue-list { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.rogue-chip {
  font-family: var(--font-mono, monospace); font-size: 12px;
  padding: 2px 8px; border-radius: 4px; background: rgba(208, 48, 80, .12);
}
.mac-tag {
  font-size: 11px; padding: 0 6px; border-radius: 3px; white-space: nowrap;
  background: var(--n-color-embedded, rgba(128, 128, 128, .12));
  color: var(--n-text-color-disabled);
}
/* 本地管理／隨機位址：標成警示色，因為它是「多半不是真衝突」的主要線索 */
.mac-tag--local { background: rgba(240, 160, 32, .16); color: #b26a00; }
</style>
