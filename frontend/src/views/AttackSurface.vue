<script setup lang="ts">
/**
 * 對外開放服務清單：從外面可達的服務，每項配 IPAM 身分。
 *
 * 異常偵測的「對外曝險」是抓問題；這一頁是**清單**——資安稽核第一個要的東西。
 * 只列明確可判定的開口；未登錄的目標用紅色標出（對外開口指向不明主機，本身就是警訊）。
 * 欄位拆開（IP／埠／主機名稱／類型／名稱／防火牆各自獨立）才能排序與比對；
 * 欄位顯示走全站的欄位偏好，來源可依防火牆廠牌篩選。
 *
 * ⚠️ API 回的是巢狀物件（identity.ip 等），進表格前先攤平成 Row——
 * 欄位 key 對不到資料列的話，autoSort 的 sorter 取到的全是 undefined，
 * 點欄位標題永遠沒反應（使用者回饋）。排序／篩選／搜尋一律吃攤平後的欄位。
 */
import { computed, onMounted, ref, watch, h } from "vue";
import {
  NCard, NSpace, NIcon, NTag, NDataTable, NEmpty, NAlert, NButton, NSelect, NInput,
  NPopover, NTabs, NTabPane, NDropdown, NModal, NTooltip, useMessage, type DataTableColumns,
} from "naive-ui";
import { useI18n } from "vue-i18n";
import { apiClient, apiErrMsg } from "@/api/client";
import { AiAuditIcon, CancelIcon, EyeIcon, ExportIcon, FirewallIcon, RefreshIcon, SearchIcon } from "@/icons";
import {
  analyzeSurfaceStream, getExternalDns, runExternalDns, type ExternalDnsName, type ExternalDnsView, type SurfaceAiEvent,
} from "@/api/attackSurface";
import { useAuthStore } from "@/stores/auth";
import { trackTask } from "@/composables/useTaskTracker";
import { renderReportPdf } from "@/api/reports";
import { useExportBusy } from "@/composables/useExportBusy";
import { exportTable } from "@/utils/tableExport";
import {
  MIME, pdfPayload, reportDocx, reportHtml, reportMarkdown, reportOdt, saveBytes, workbookOds, workbookXlsx,
  type Report, type ReportSection, type Sheet,
} from "@/utils/reportExport";
import { PDF_MAX, clipForPdf, pdfProblem, suggestWidths } from "@/utils/anomalyExport";
import { markdownSections } from "@/utils/attackSurfaceExport";
import { renderMarkdown } from "@/utils/markdown";
import { aiErrText } from "@/utils/wsError";
import { fmtDateTime } from "@/utils/datetime";
import { autoSort } from "@/composables/useTableSort";
import { useTablePagination } from "@/composables/useTablePagination";
import { useColumnPrefs } from "@/composables/useColumnPrefs";
import ColumnPicker from "@/components/ColumnPicker.vue";
import { useRoute, useRouter } from "vue-router";
import { useEntityLinks } from "@/composables/useEntityLinks";
import { deviceKindColumn } from "@/utils/deviceKindCell";

const { t, te, locale } = useI18n();
const msg = useMessage();
const pg = useTablePagination();
const links = useEntityLinks(useRouter());

/** 這個埠上跑的是什麼（後端 surface_service：已確認／依名稱／依埠號推測，2026-10-19） */
interface ServiceInfo {
  name: string | null; level: "confirmed" | "named" | "port_guess" | null; evidence: string[];
  sensitive: boolean; unverified_sensitive: boolean; mismatch: string | null;
}
interface Entry {
  key?: string;
  service?: ServiceInfo;
  via: string; source: string; firewall: string | null; name: string;
  protocol: string | null; port: number | string | null; descr: string;
  identity: { registered: boolean; ip?: string; ip_id?: string; hostname?: string | null;
              status?: string | null; subnet?: string | null;
              customer?: string | null; wazuh?: string | null; fqdns?: string[];
              device_kind?: string | null; device_model?: string | null };
}
/** 攤平後的表格列：欄位 key 與 column key 一一對應，排序／搜尋才有得比。 */
interface Row {
  ip: string | null; ip_id?: string; registered: boolean;
  port: number | null; portText: string;
  hostname: string | null; device_kind: string | null; device_model: string | null; via: string; name: string;
  firewall: string | null; source: string; protocol: string | null;
  owner: string | null; wazuh: string | null;
  status: string | null; online: boolean | null; statusSrc: string;
  descr: string; key: string; fqdns: string[];
  /** 後端給的列識別（AI 分析時送回畫面上那些列；後端照它重挑） */
  akey: string;
  service: ServiceInfo | null;
  /** 這一列的穩定識別。**不可以用陣列索引** —— 過濾之後同一個索引會指到不同的列，
   *  表格會拿舊的列來重用，於是被篩掉的列還留在畫面上（實機遇過：搜尋 A 卻看到 B）。 */
  uid: string;
}
const items = ref<Entry[]>([]);
const loading = ref(false);
const aiAvailable = ref(false);
const auth = useAuthStore();
const route = useRoute();

// ── 外部 DNS 解析（FQDN 頁籤；使用者 2026-10-19 定案）──
const ext = ref<ExternalDnsView | null>(null);
const extRunning = ref(false);
async function loadExternal() {
  try { ext.value = await getExternalDns(); } catch { ext.value = null; }
}
async function runExternal() {
  extRunning.value = true;
  try {
    const r = await runExternalDns();
    trackTask(r.task_id, { onDone: () => { extRunning.value = false; void loadExternal(); } });
  } catch (e) { extRunning.value = false; msg.error(apiErrMsg(e)); }
}
const extOf = (fqdn: string): ExternalDnsName | undefined => ext.value?.names[fqdn.toLowerCase().replace(/\.$/, "")];
const EXT_TONE: Record<string, "success" | "warning" | "error" | "default" | "info"> = {
  consistent: "success", not_public: "default", foreign: "error", mismatch: "error", no_path: "warning",
  query_failed: "warning", outside_domains: "default", not_checked: "default",
};

async function load() {
  loading.value = true;
  try {
    const { data } = await apiClient.get("/api/v1/anomalies/attack-surface");
    items.value = data.items ?? [];
    aiAvailable.value = !!data.ai_available;
  } catch (e: any) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}
onMounted(() => {
  if (route.query.tab === "fqdn") tab.value = "fqdn";   // 外部 DNS 變動通知的連結會帶 ?tab=fqdn
  void load();
  void loadExternal();
});

const rows = computed<Row[]>(() => items.value.map((i, idx) => {
  const ident = i.identity ?? { registered: false };
  const raw = ident.status ?? null;
  const online = raw ? raw.toLowerCase().startsWith("online") : null;
  const pn = Number(i.port);
  return {
    ip: ident.ip ?? null, ip_id: ident.ip_id, registered: !!ident.registered,
    // 埠轉成數字才能正確排序（來源混雜 int 與字串；空字串／區間視為無埠）
    port: i.port == null || i.port === "" || Number.isNaN(pn) ? null : pn,
    portText: i.port == null || i.port === "" ? "" : String(i.port),
    hostname: ident.hostname ?? null,
    device_kind: ident.device_kind ?? null, device_model: ident.device_model ?? null,
    via: i.via, name: i.name || "", firewall: i.firewall, source: i.source,
    // 協定統一大寫（NAT 同步存小寫、規則存大寫，混著顯示很刺眼——使用者回饋）
    protocol: i.protocol ? String(i.protocol).toUpperCase() : null,
    owner: ident.customer || ident.subnet || null,
    wazuh: ident.wazuh ?? null,
    status: raw ? (online ? t("surface.st_online") : t("surface.st_offline")) : null,
    online, statusSrc: raw ? ((raw.match(/\(([^)]+)\)/)?.[1]) ?? "") : "",
    descr: i.descr || "",
    fqdns: ident.fqdns ?? [],
    key: `${ident.ip ?? "?"}:${i.port ?? ""}`,
    akey: i.key ?? "",
    service: i.service ?? null,
    // 在**未過濾**的來源清單裡的位置 —— 只有重新載入資料才會變
    uid: `r${idx}`,
  };
}));

// 篩選：選項一律由資料動態產生（新增廠牌／協定／單位都不必改程式）。
// 各下拉與搜尋框是「且」的關係，疊加過濾。
const sourceFilter = ref<string | null>(null);
// 防火牆「實例」的篩選。原本只能選類型（opnsense / pfsense…），但同一種類型底下
// 常常有好幾台，而使用者要問的是「**這一台**開了哪些對外服務」。
const firewallFilter = ref<string | null>(null);
const viaFilter = ref<string | null>(null);
const protoFilter = ref<string | null>(null);
const statusFilter = ref<string | null>(null);
const ownerFilter = ref<string | null>(null);
const searchText = ref("");

// 換了類型而原本選的那一台不屬於它時，把實例選擇清掉 —— 留著會得到一張空表格，
// 而畫面上沒有任何地方說明為什麼。
watch(sourceFilter, () => {
  if (!firewallFilter.value || firewallFilter.value === "__all__") return;
  const stillThere = rows.value.some(
    (r) => pick(sourceFilter.value, r.source) && r.firewall === firewallFilter.value);
  if (!stillThere) firewallFilter.value = null;
});

function opts(values: (string | null | undefined)[], allLabel: string) {
  const seen = [...new Set(values.filter((v): v is string => !!v))].sort();
  return [{ label: allLabel, value: "__all__" }, ...seen.map((v) => ({ label: v, value: v }))];
}
const sourceOptions = computed(() => opts(rows.value.map((r) => r.source), t("surface.all_sources")));
// 選了類型之後，實例清單只列那個類型底下的 —— 選項裡出現「不可能有結果」的組合，
// 使用者選下去只會看到空白表格而沒有任何解釋。
const firewallOptions = computed(() => opts(
  rows.value.filter((r) => pick(sourceFilter.value, r.source)).map((r) => r.firewall),
  t("surface.all_firewalls")));
const viaOptions = computed(() => [
  { label: t("surface.all_via"), value: "__all__" },
  { label: "NAT", value: "nat" }, { label: t("surface.rule"), value: "rule" },
]);
const protoOptions = computed(() => opts(rows.value.map((r) => r.protocol), t("surface.all_proto")));
const statusOptions = computed(() => opts(rows.value.map((r) => r.status), t("surface.all_status")));
const ownerOptions = computed(() => opts(rows.value.map((r) => r.owner), t("surface.all_owner")));

// NAT ↔ 規則配對：pfSense/OPNsense 的埠轉發常帶一條關聯放行規則。
// 同目標 IP＋同埠同時存在 NAT 列與規則列 → 兩列都標「配對」，一眼看出它們是一組。
// 純資料比對，不猜語意；對不上的（例如規則沒寫埠）就不標。
const pairedKeys = computed(() => {
  const nat = new Set<string>(), rule = new Set<string>();
  for (const r of rows.value) (r.via === "nat" ? nat : rule).add(r.key);
  return new Set([...nat].filter((k) => rule.has(k)));
});
const isPaired = (r: Row) => pairedKeys.value.has(r.key);

// 配對標籤要說得出「跟誰配對」（使用者回饋）：同鍵的其它列就是配對對象，
// hover／點擊標籤彈出卡片列出來（類型＋名稱＋防火牆）。
const pairMap = computed(() => {
  const m = new Map<string, Row[]>();
  for (const r of rows.value) {
    if (!pairedKeys.value.has(r.key)) continue;
    if (!m.has(r.key)) m.set(r.key, []);
    m.get(r.key)!.push(r);
  }
  return m;
});
const counterparts = (r: Row): Row[] =>
  (pairMap.value.get(r.key) ?? []).filter((x) => x !== r);

const pick = (f: string | null, v: string | null | undefined) =>
  !f || f === "__all__" || (v ?? "") === f;
const shown = computed(() => {
  let out = rows.value.filter((r) =>
    pick(sourceFilter.value, r.source)
    && pick(firewallFilter.value, r.firewall)
    && pick(viaFilter.value, r.via)
    && pick(protoFilter.value, r.protocol)
    && pick(statusFilter.value, r.status)
    && pick(ownerFilter.value, r.owner));
  const q = searchText.value.trim().toLowerCase();
  if (q) {
    out = out.filter((r) =>
      [r.ip, r.hostname, r.name, r.descr, r.portText, r.firewall, r.owner,
       r.fqdns.join(" ")]
        .some((v) => (v ?? "").toString().toLowerCase().includes(q)));
  }
  return out;
});

// ── FQDN 視角 ───────────────────────────────────────────────
// 稽核與對外服務盤點時，人記得的是名字不是位址（「meet 對外開了什麼」）。
// 名稱來自 IPAM 已同步的 DNS 記錄（A/AAAA 直接命中、CNAME 別名往上追），
// 不做即時解析 —— 清單要可重現，不能因為外部 DNS 變動而每次不同。
interface FqdnRow {
  fqdn: string; ip: string | null; ip_id?: string; registered: boolean;
  ports: string; portCount: number; owner: string | null;
  status: string | null; online: boolean | null;
  firewalls: string; names: string; key: string;
  extStatus: string; extAddrs: string; extOwner: string; extChecked: string | null; ext?: ExternalDnsName;
}
const fqdnRows = computed<FqdnRow[]>(() => {
  const by = new Map<string, Row[]>();
  for (const r of shown.value) {
    for (const f of r.fqdns) {
      if (!by.has(f)) by.set(f, []);
      by.get(f)!.push(r);
    }
  }
  return [...by.entries()].map(([fqdn, rs]) => {
    const ports = [...new Set(rs.map((r) => (r.protocol ? `${r.protocol.toLowerCase()}/` : "")
                                            + (r.portText || "—")))].sort();
    const first = rs[0];
    return {
      fqdn, ip: first.ip, ip_id: first.ip_id, registered: first.registered,
      ports: ports.join("、"), portCount: ports.length,
      owner: first.owner, status: first.status, online: first.online,
      firewalls: [...new Set(rs.map((r) => r.firewall).filter(Boolean))].join("、"),
      names: [...new Set(rs.map((r) => r.name).filter(Boolean))].join("、"),
      key: fqdn,
      ...extFields(fqdn),
    };
  }).sort((a, b) => a.fqdn.localeCompare(b.fqdn));
});
function extFields(fqdn: string) {
  const e = extOf(fqdn);
  const status = e?.status ?? "not_checked";
  const owners = e ? [...new Set(e.addresses.map((a) => e.owners[a] ?? t("surface.ext_not_ours")))] : [];
  return { ext: e, extStatus: status, extAddrs: e?.addresses.join("、") ?? "",
           extOwner: owners.join("、"), extChecked: e?.checked_at ?? null };
}
/** 外部解析的明細（滑過看）：每個解析器／權威伺服器各自的答案 */
function extDetail(e: ExternalDnsName) {
  return h("div", { style: "font-size:12.5px; line-height:1.8; max-width: 420px" }, e.answers.map((a) =>
    h("div", null, [
      h("b", null, a.authoritative ? `${t("surface.ext_authoritative")} ${a.resolver.replace(/^authoritative:/, "")}` : a.resolver),
      "：", [...a.a, ...a.aaaa].join("、") || a.rcode,
      a.cname.length ? h("span", { style: "opacity:.6" }, ` （CNAME ${a.cname.join(" → ")}）`) : null,
      a.error ? h("span", { style: "opacity:.6" }, ` ${a.error}`) : null,
    ])));
}
const extFilter = ref<string | null>(null);
const extStatusOptions = computed(() => [{ label: t("surface.ext_all_status"), value: "__all__" },
  ...["consistent", "not_public", "foreign", "mismatch", "no_path", "query_failed", "outside_domains", "not_checked"]
    .map((v) => ({ label: t(`surface.ext_st_${v}`), value: v }))]);
const fqdnShown = computed(() => (!extFilter.value || extFilter.value === "__all__")
  ? fqdnRows.value : fqdnRows.value.filter((r) => r.extStatus === extFilter.value));

// 有對外開口、卻沒有任何 DNS 名稱對應 —— FQDN 視角看不到它們，要明講有幾筆，
// 否則使用者會以為「FQDN 頁籤的筆數就是全部」。
const noFqdnCount = computed(() => shown.value.filter((r) => !r.fqdns.length).length);

const fqdnCols = computed<DataTableColumns<FqdnRow>>(() => autoSort([
  { title: () => t("surface.col_fqdn"), key: "fqdn", width: 260, ellipsis: { tooltip: true } },
  { title: () => t("surface.col_ip"), key: "ip", width: 150,
    render: (r: FqdnRow) => r.registered && r.ip_id
      ? links.ipById(r.ip_id, r.ip ?? "") : (r.ip ?? "—") },
  // 外部 DNS：從網際網路上查這個名稱得到什麼、那個位址是誰的、跟內部一不一致
  ...(ext.value?.configured ? [
    { title: () => t("surface.col_ext_addrs"), key: "extAddrs", width: 170,
      render: (r: FqdnRow) => r.ext && r.ext.answers.length
        ? h(NPopover, { trigger: "hover" }, { trigger: () => h("span", { style: "cursor: help" }, r.extAddrs || "—"),
                                             default: () => extDetail(r.ext!) })
        : "—" },
    { title: () => t("surface.col_ext_owner"), key: "extOwner", width: 190, ellipsis: { tooltip: true },
      render: (r: FqdnRow) => r.extOwner || "—" },
    { title: () => t("surface.col_ext_status"), key: "extStatus", width: 150,
      render: (r: FqdnRow) => h(NTag, { size: "small", type: EXT_TONE[r.extStatus] ?? "default", bordered: true },
                                { default: () => t(`surface.ext_st_${r.extStatus}`) }) },
  ] : []),
  { title: () => t("surface.col_ports"), key: "ports", minWidth: 200,
    ellipsis: { tooltip: true }, render: (r: FqdnRow) => r.ports || "—" },
  { title: () => t("surface.col_name"), key: "names", minWidth: 200,
    ellipsis: { tooltip: true }, render: (r: FqdnRow) => r.names || "—" },
  { title: () => t("surface.col_firewall"), key: "firewalls", width: 180,
    render: (r: FqdnRow) => r.firewalls || "—" },
  // 「單位／網段」跟著 IP 檢視的欄位設定（預設不顯示，使用者 2026-10-19）
  ...(visibleKeys.value.includes("owner") ? [{ title: () => t("surface.col_owner"), key: "owner", width: 190,
    render: (r: FqdnRow) => r.owner || "—" }] : []),
  { title: () => t("surface.col_status"), key: "status", width: 130,
    render: (r: FqdnRow) => r.status
      ? h("span", { style: "display:inline-flex;align-items:center;gap:6px" }, [
          h("i", { style: `width:8px;height:8px;border-radius:50%;flex:none;background:${r.online ? "#22c55e" : "#ef4444"}` }),
          r.status,
        ])
      : "—" },
  ...(ext.value?.configured ? [
    { title: () => t("surface.col_ext_checked"), key: "extChecked", width: 160,
      render: (r: FqdnRow) => r.extChecked ? fmtDateTime(r.extChecked) : "—" },
  ] : []),
]));
const fqdnScrollX = computed(() => fqdnCols.value.reduce((sum, c: any) => sum + (c.width ?? c.minWidth ?? 160), 0));
const tab = ref<"ip" | "fqdn">("ip");

const ALL_KEYS = ["ip", "port", "service", "hostname", "device_kind", "via", "name", "firewall",
                  "protocol", "owner", "wazuh", "status", "descr"];
// 「單位／網段」預設不顯示（使用者 2026-10-19）；要看的人從「欄位」打開
const DEFAULT_KEYS = ALL_KEYS.filter((k) => k !== "owner");
const { visibleKeys, setVisible, reset, order, setOrder, orderColumns } =
  useColumnPrefs("attack_surface", ALL_KEYS, DEFAULT_KEYS);
const pickerCols = computed(() => ALL_KEYS.map((k) => ({
  key: k, label: k === "device_kind" ? t("cols.device_kind") : t(`surface.col_${k}`) })));

const allCols: Record<string, any> = {
  ip: { title: () => t("surface.col_ip"), key: "ip", width: 150,
    render: (r: Row) => r.registered
      // IPAM 有這筆 → 點過去 IP 卡片（全站同一套 entity link）
      ? (r.ip_id ? links.ipById(r.ip_id, r.ip ?? "") : r.ip)
      : h("span", null, ["?", h(NTag, { size: "tiny", type: "error", style: "margin-left:6px" },
          { default: () => t("surface.unregistered") })]) },
  port: { title: () => t("surface.col_port"), key: "port", width: 90,
    render: (r: Row) => r.portText || "—" },
  // 這個埠上跑的是什麼，附證據等級；只有埠號推測的不當事實（Jitsi 的 UDP 10000 曾被講成 Webmin）
  service: { title: () => t("surface.col_service"), key: "service", width: 210,
    sorter: (a: Row, b: Row) => (a.service?.name ?? "").localeCompare(b.service?.name ?? ""),
    render: (r: Row) => {
      const s = r.service;
      const svcName = s?.name;
      if (!s || !svcName) return "—";
      const tone = s.level === "confirmed" ? "success" : s.level === "named" ? "info" : "default";
      return h(NPopover, { trigger: "hover" }, {
        trigger: () => h("span", { style: "display:inline-flex;align-items:center;gap:6px;cursor:help" }, [
          h("span", null, svcName),
          h(NTag, { size: "tiny", type: tone, bordered: true }, { default: () => t(`surface.svc_${s.level}`) }),
          s.mismatch ? h(NTag, { size: "tiny", type: "error", bordered: true }, { default: () => t("surface.svc_mismatch") }) : null,
          s.sensitive || s.unverified_sensitive
            ? h("span", { style: `font-size:11px;color:${s.sensitive ? "#d03050" : "#999"}` },
                s.sensitive ? t("surface.svc_sensitive") : t("surface.svc_unverified")) : null,
        ]),
        default: () => h("div", { style: "font-size:12.5px;line-height:1.8;max-width:420px" }, [
          ...(s.mismatch ? [h("div", { style: "color:#d03050" }, s.mismatch)] : []),
          ...s.evidence.map((e) => h("div", null, e)),
        ]),
      });
    } },
  hostname: { title: () => t("surface.col_hostname"), key: "hostname", width: 160,
    render: (r: Row) => r.hostname || "—" },
  // 對外開的是攝影機、NAS 還是伺服器（掃描代理判讀出的設備類型，滑過看型號）
  device_kind: deviceKindColumn(t, te),
  via: { title: () => t("surface.col_via"), key: "via", width: 130,
    render: (r: Row) => h("span", { style: "display:inline-flex;align-items:center;gap:6px" }, [
      r.via === "nat" ? "NAT" : t("surface.rule"),
      isPaired(r) ? h(NPopover, { trigger: "hover", placement: "right" }, {
        trigger: () => h(NTag, { size: "tiny", type: "info", bordered: false,
                                 style: "cursor: pointer" },
                         { default: () => t("surface.paired") }),
        default: () => h("div", { style: "max-width: 340px; font-size: 12.5px; line-height: 1.8" }, [
          h("div", { style: "opacity:.7; margin-bottom: 2px" }, t("surface.paired_with")),
          ...counterparts(r).map((c) => h("div",
            { style: "display:flex;align-items:center;gap:6px" }, [
              h(NTag, { size: "tiny", type: c.via === "nat" ? "warning" : "success",
                        bordered: false, style: "flex:none" },
                { default: () => c.via === "nat" ? "NAT" : t("surface.rule") }),
              h("span", null, c.name || "—"),
              c.firewall ? h("span", { style: "opacity:.6" }, `（${c.firewall}）`) : null,
            ])),
          h("div", { style: "opacity:.6; margin-top: 4px; font-size: 11.5px" },
            t("surface.paired_tip")),
        ]),
      }) : null,
    ]) },
  name: { title: () => t("surface.col_name"), key: "name", width: 220,
    ellipsis: { tooltip: true }, render: (r: Row) => r.name || "—" },
  firewall: { title: () => t("surface.col_firewall"), key: "firewall", width: 180,
    render: (r: Row) => h("span", null, [
      h(NTag, { size: "tiny", style: "margin-right:6px" }, { default: () => r.source }),
      r.firewall || "—",
    ]) },
  protocol: { title: () => t("surface.col_protocol"), key: "protocol", width: 90,
    render: (r: Row) => r.protocol || "—" },
  owner: { title: () => t("surface.col_owner"), key: "owner", width: 190,
    render: (r: Row) => r.owner || "—" },
  wazuh: { title: "Wazuh", key: "wazuh", width: 110,
    render: (r: Row) => r.registered
      ? (r.wazuh
          ? h(NTag, { size: "tiny", type: "success" }, { default: () => r.wazuh })
          : h("span", { style: "opacity:.6" }, t("surface.no_agent")))
      : "—" },
  status: { title: () => t("surface.col_status"), key: "status", width: 130,
    render: (r: Row) => {
      if (!r.status) return "—";
      return h("span", { style: "display:inline-flex;align-items:center;gap:6px" }, [
        h("i", { style: `width:8px;height:8px;border-radius:50%;flex:none;background:${r.online ? "#22c55e" : "#ef4444"}` }),
        r.status,
        r.statusSrc ? h("span", { style: "opacity:.55;font-size:11.5px" }, r.statusSrc) : null,
      ]);
    } },
  descr: { title: () => t("surface.col_descr"), key: "descr", minWidth: 200,
    ellipsis: { tooltip: true } },
};
const cols = computed<DataTableColumns<Row>>(
  () => autoSort(orderColumns(ALL_KEYS.filter((k) => visibleKeys.value.includes(k)).map((k) => allCols[k]))));
// 表格要在卡片內水平捲動，不能溢出卡片右緣（使用者截圖）：
// scroll-x 取「可見欄寬總和」，欄位少的時候不強撐寬度。
const scrollX = computed(() =>
  ALL_KEYS.filter((k) => visibleKeys.value.includes(k))
    .reduce((sum, k) => sum + (allCols[k].width ?? allCols[k].minWidth ?? 160), 0));

// ── 匯出（使用者 2026-10-19：pdf html odt ods docx xlsx csv markdown json）─────────────
// 內容＝畫面上看到的：IP 檢視的可見欄位、FQDN 檢視、篩選條件；跑過 AI 分析就一併附上。
const exportOptions = computed(() => [
  { type: "group", key: "g-report", label: t("surface.export_group_report"), children: [
    { label: "PDF", key: "pdf" }, { label: "HTML", key: "html" },
    { label: "Word (.docx)", key: "docx" }, { label: "OpenDocument (.odt)", key: "odt" }] },
  { type: "group", key: "g-table", label: t("surface.export_group_table"), children: [
    { label: "Excel (.xlsx)", key: "xlsx" }, { label: t("export.fmt_ods_sheet"), key: "ods" }] },
  { type: "group", key: "g-data", label: t("surface.export_group_data"), children: [
    { label: t("surface.export_csv_tab"), key: "csv" }, { label: "Markdown", key: "md" }, { label: "JSON", key: "json" }] },
]);
const visibleIpKeys = computed(() => orderColumns(ALL_KEYS.filter((k) => visibleKeys.value.includes(k))
  .map((k) => allCols[k])).map((c: any) => c.key as string));
function ipColLabel(k: string): string {
  return k === "device_kind" ? t("cols.device_kind") : k === "wazuh" ? "Wazuh" : t(`surface.col_${k}`);
}
function ipCell(r: Row, k: string): string {
  switch (k) {
    case "ip": return r.registered ? (r.ip ?? "") : `? (${t("surface.unregistered")})`;
    case "port": return r.portText;
    case "service": return r.service?.name
      ? `${r.service.name}（${t(`surface.svc_${r.service.level}`)}）${r.service.mismatch ? `；${r.service.mismatch}` : ""}`
      : "";
    case "device_kind": return r.device_kind ? (te(`device_kind.${r.device_kind}`) ? t(`device_kind.${r.device_kind}`) : r.device_kind) : "";
    case "via": return (r.via === "nat" ? "NAT" : t("surface.rule")) + (isPaired(r) ? ` (${t("surface.paired")})` : "");
    case "firewall": return [r.source, r.firewall].filter(Boolean).join(" ");
    case "wazuh": return r.registered ? (r.wazuh || t("surface.no_agent")) : "";
    case "status": return [r.status, r.statusSrc].filter(Boolean).join(" ");
    default: return String((r as any)[k] ?? "");
  }
}
const fqdnExportCols = computed(() => ["fqdn", "ip",
  ...(ext.value?.configured ? ["extAddrs", "extOwner", "extStatus"] : []), "ports", "names", "firewalls",
  ...(visibleKeys.value.includes("owner") ? ["owner"] : []), "status",
  ...(ext.value?.configured ? ["extChecked"] : [])]);
const FQDN_LABEL_KEY: Record<string, string> = { names: "name", firewalls: "firewall", extAddrs: "ext_addrs",
  extOwner: "ext_owner", extStatus: "ext_status", extChecked: "ext_checked" };
const fqdnLabel = (k: string) => t(`surface.col_${FQDN_LABEL_KEY[k] ?? k}`);
const fqdnCell = (r: FqdnRow, k: string) => k === "extStatus" ? t(`surface.ext_st_${r.extStatus}`)
  : k === "extChecked" ? (r.extChecked ? fmtDateTime(r.extChecked) : "") : String((r as any)[k] ?? "");
function filterSummary(): string {
  const f: string[] = [];
  const add = (v: string | null, label: string) => { if (v && v !== "__all__") f.push(`${label}：${v}`); };
  add(sourceFilter.value, t("surface.all_sources")); add(firewallFilter.value, t("surface.all_firewalls"));
  add(viaFilter.value, t("surface.all_via")); add(protoFilter.value, t("surface.all_proto"));
  add(statusFilter.value, t("surface.all_status")); add(ownerFilter.value, t("surface.all_owner"));
  if (extFilter.value && extFilter.value !== "__all__") f.push(`${t("surface.col_ext_status")}：${t(`surface.ext_st_${extFilter.value}`)}`);
  if (searchText.value.trim()) f.push(`${t("common.search")}：${searchText.value.trim()}`);
  return f.join("；") || t("surface.export_no_filter");
}
const exportBase = () => `attack-surface-${new Date().toISOString().slice(0, 10)}`;
function buildReport(): Report {
  // 欄寬照內容分配（平均分的話 IP 會在中間被折斷）
  const ipCols = visibleIpKeys.value.map(ipColLabel);
  const ipRows = shown.value.map((r) => visibleIpKeys.value.map((k) => ipCell(r, k)));
  const fqCols = fqdnExportCols.value.map(fqdnLabel);
  const fqRows = fqdnShown.value.map((r) => fqdnExportCols.value.map((k) => fqdnCell(r, k)));
  const sections: ReportSection[] = [
    { heading: `${t("surface.tab_ip")} (${shown.value.length})`,
      table: { cols: ipCols, rows: ipRows, widths: suggestWidths(ipCols, ipRows),
               tones: shown.value.map((r) => (r.registered ? null : "danger")) } },
    { heading: `${t("surface.tab_fqdn")} (${fqdnShown.value.length})`,
      table: { cols: fqCols, rows: fqRows, widths: suggestWidths(fqCols, fqRows) },
      caption: noFqdnCount.value ? t("surface.no_fqdn_note", { n: noFqdnCount.value }) : undefined },
  ];
  if (aiText.value) {
    sections.push(...markdownSections(aiText.value, t("surface.ai_title")));
    sections.push({ heading: t("surface.ai_about"), paragraphs: [
      `${t("surface.ai_model")}：${aiModel.value || "—"}`, t("surface.ai_disclaimer")] });
  }
  return {
    title: t("surface.title"), generatedAt: fmtDateTime(new Date()),
    meta: [[t("surface.export_generated"), fmtDateTime(new Date())], [t("surface.export_filters"), filterSummary()],
           [t("surface.export_counts"), t("surface.export_counts_v", { ip: shown.value.length, fqdn: fqdnShown.value.length })]],
    sections, note: t("surface.scope_note"),
    // 欄位多時用橫式：直式放 11 欄，NAT、TCP 這種短欄會被折成一字一行
    landscape: ipCols.length > 8,
  };
}
function buildSheets(): Sheet[] {
  return [
    { name: t("surface.tab_ip"), cols: visibleIpKeys.value.map(ipColLabel),
      rows: shown.value.map((r) => visibleIpKeys.value.map((k) => ipCell(r, k))) },
    { name: t("surface.tab_fqdn"), cols: fqdnExportCols.value.map(fqdnLabel),
      rows: fqdnShown.value.map((r) => fqdnExportCols.value.map((k) => fqdnCell(r, k))) },
  ];
}
const { exporting, run: runExport } = useExportBusy();
async function doExport(fmt: string) {
  if (!shown.value.length) { msg.warning(t("export.empty")); return; }
  const base = exportBase();
  const meta = { source: "attack-surface", rows: shown.value.length };
  try {
    await runExport(async () => {
      if (fmt === "csv") {   // CSV 一次只能一張表：匯出目前的頁籤
        if (tab.value === "fqdn") {
          exportTable("csv", `${base}-fqdn`, fqdnExportCols.value.map((k) => ({ key: k, label: fqdnLabel(k),
            value: (r: Record<string, any>) => fqdnCell(r as FqdnRow, k) })), fqdnShown.value);
        } else {
          exportTable("csv", `${base}-ip`, visibleIpKeys.value.map((k) => ({ key: k, label: ipColLabel(k),
            value: (r: Record<string, any>) => ipCell(r as Row, k) })), shown.value);
        }
        return;
      }
      if (fmt === "json") {
        const data = {
          title: t("surface.title"), generated_at: new Date().toISOString(), filters: filterSummary(),
          items: shown.value.map((r) => ({ ip: r.ip, registered: r.registered, port: r.portText, protocol: r.protocol,
            hostname: r.hostname, device_kind: r.device_kind, via: r.via, paired: isPaired(r), name: r.name,
            service: r.service,
            firewall: r.firewall, source: r.source, owner: r.owner, wazuh: r.wazuh, online: r.online,
            status_source: r.statusSrc || null, description: r.descr, fqdns: r.fqdns })),
          fqdns: fqdnShown.value.map((r) => ({ fqdn: r.fqdn, ip: r.ip, registered: r.registered, ports: r.ports,
            names: r.names, firewalls: r.firewalls, owner: r.owner, online: r.online,
            external: r.ext ? { status: r.extStatus, addresses: r.ext.addresses, owners: r.ext.owners,
                                answers: r.ext.answers, checked_at: r.extChecked } : null })),
          ai_analysis: aiText.value ? { text: aiText.value, model: aiModel.value, facts: aiFacts.value } : null,
        };
        saveBytes(`${base}.json`, new TextEncoder().encode(JSON.stringify(data, null, 2)), "application/json", meta);
        return;
      }
      if (fmt === "xlsx" || fmt === "ods") {
        saveBytes(`${base}.${fmt}`, fmt === "xlsx" ? workbookXlsx(buildSheets()) : workbookOds(buildSheets()), MIME[fmt], meta);
        return;
      }
      const rep = buildReport();
      if (fmt === "md") {
        saveBytes(`${base}.md`, new TextEncoder().encode(reportMarkdown(rep)), "text/markdown", meta);
        return;
      }
      if (fmt === "html") {
        saveBytes(`${base}.html`, new TextEncoder().encode(reportHtml(rep, String(locale.value))), MIME.html, meta);
        return;
      }
      if (fmt === "pdf") {
        const problem = pdfProblem(rep);
        if (problem) {
          const max = problem.kind === "cols" ? PDF_MAX.cols : problem.kind === "rows" ? PDF_MAX.rows : PDF_MAX.cells;
          msg.warning(t(`anomaly.export_pdf_${problem.kind}`, { ...problem, max }), { duration: 8000 });
          return;
        }
        // 後端排版成 PDF（內嵌中文字型）；伺服器排版時已記稽核
        const blob = await renderReportPdf(pdfPayload(clipForPdf(rep), String(locale.value), base));
        saveBytes(`${base}.pdf`, new Uint8Array(await blob.arrayBuffer()), "application/pdf", { ...meta, audited: true });
        return;
      }
      saveBytes(`${base}.${fmt}`, fmt === "docx" ? reportDocx(rep) : reportOdt(rep), MIME[fmt as "docx" | "odt"], meta);
    });
  } catch (e) { msg.error(apiErrMsg(e)); }
}

// ── AI 分析（使用者 2026-10-19）────────────────────────────────────────────
// 分析畫面上（篩選後）的那些列；統計由後端算、模型只寫判讀，邊寫邊顯示。結果留在頁面可重看、可匯出。
const aiOpen = ref(false);
const aiRunning = ref(false);
const aiText = ref("");
const aiModel = ref("");
const aiFacts = ref<SurfaceAiEvent["facts"] | null>(null);
const aiError = ref("");
const aiElapsed = ref(0);
const aiRows = ref(0);
let aiAbort: AbortController | null = null;
async function runAi() {
  if (!shown.value.length) { msg.warning(t("export.empty")); return; }
  aiAbort?.abort();
  aiAbort = new AbortController();
  aiOpen.value = true; aiRunning.value = true;
  aiText.value = ""; aiModel.value = ""; aiFacts.value = null; aiError.value = ""; aiElapsed.value = 0;
  aiRows.value = shown.value.length;
  const all = shown.value.length === rows.value.length;
  try {
    await analyzeSurfaceStream(all ? null : shown.value.map((r) => r.akey).filter(Boolean), (ev) => {
      if (ev.elapsed != null) aiElapsed.value = ev.elapsed;
      if (ev.type === "content" && ev.text) aiText.value += ev.text;
      else if (ev.type === "done") {
        aiText.value = ev.text || aiText.value; aiModel.value = ev.model ?? ""; aiFacts.value = ev.facts ?? null;
      } else if (ev.type === "error") aiError.value = aiErrText(ev, ev.detail ?? "");
    }, aiAbort.signal);
  } catch (e: any) {
    if (e?.name !== "AbortError") aiError.value = e?.message || t("errors.network");
  } finally { aiRunning.value = false; }
}
function stopAi() { aiAbort?.abort(); aiRunning.value = false; }
const aiHtml = computed(() => renderMarkdown(aiText.value));
</script>

<template>
  <n-card :bordered="false">
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><FirewallIcon /></n-icon>
        <span>{{ t("surface.title") }}</span>
      </n-space>
    </template>
    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("surface.hint") }}<br>{{ t("surface.scope_note") }}
    </n-alert>
    <!-- 工具列與其它頁一致：放內文、不佔卡片標題列；全部 size=small 高度對齊 -->
    <n-space align="center" style="margin-bottom: 10px">
      <n-input v-model:value="searchText" clearable style="width: 220px"
               :placeholder="t('surface.search_ph')">
        <template #prefix><n-icon><SearchIcon /></n-icon></template>
      </n-input>
      <n-select v-model:value="sourceFilter" :options="sourceOptions"
                style="width: 150px"
                :placeholder="t('surface.all_sources')" clearable />
      <n-select v-model:value="firewallFilter" :options="firewallOptions" style="width: 170px"
                :placeholder="t('surface.all_firewalls')" filterable clearable />
      <n-select v-model:value="viaFilter" :options="viaOptions" style="width: 120px"
                :placeholder="t('surface.all_via')" clearable />
      <n-select v-model:value="protoFilter" :options="protoOptions" style="width: 120px"
                :placeholder="t('surface.all_proto')" clearable />
      <n-select v-model:value="statusFilter" :options="statusOptions" style="width: 140px"
                :placeholder="t('surface.all_status')" clearable />
      <n-select v-model:value="ownerFilter" :options="ownerOptions" style="width: 170px"
                :placeholder="t('surface.all_owner')" clearable />
      <ColumnPicker :all="pickerCols" :visible="visibleKeys"
                    @update:visible="setVisible" @reset="reset"
                    :order="order" @update:order="setOrder" />
      <n-button size="small" :loading="loading" @click="() => { void load(); void loadExternal(); }">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
      <n-dropdown :options="exportOptions" :disabled="exporting" trigger="click" @select="doExport">
        <n-button size="small" :loading="exporting" :disabled="exporting" data-testid="surface-export">
          <template #icon><n-icon><ExportIcon /></n-icon></template>
          {{ t("surface.export") }}
        </n-button>
      </n-dropdown>
      <n-tooltip :disabled="aiAvailable">
        <template #trigger>
          <n-button size="small" type="primary" secondary :disabled="!aiAvailable || aiRunning" :loading="aiRunning"
                    data-testid="surface-ai" @click="runAi">
            <template #icon><n-icon><AiAuditIcon /></n-icon></template>
            {{ t("surface.ai_btn") }}
          </n-button>
        </template>
        {{ t("surface.ai_unavailable") }}
      </n-tooltip>
      <n-button v-if="(aiText || aiError) && !aiOpen" size="small" data-testid="surface-ai-view" @click="aiOpen = true">
        <template #icon><n-icon><EyeIcon /></n-icon></template>
        {{ t("surface.ai_view") }}
      </n-button>
    </n-space>
    <n-tabs v-model:value="tab" type="line" animated size="small">
      <n-tab-pane name="ip" :tab="`${t('surface.tab_ip')} (${shown.length})`">
        <n-data-table :columns="cols" :data="shown" :loading="loading" size="small"
                      :scroll-x="scrollX"
                      :row-key="(r: Row) => r.uid"
                      :pagination="pg" :bordered="false" />
        <n-empty v-if="!loading && !shown.length" style="margin: 24px 0"
                 :description="t('surface.empty')" />
      </n-tab-pane>
      <n-tab-pane name="fqdn" :tab="`${t('surface.tab_fqdn')} (${fqdnShown.length})`">
        <!-- 外部 DNS：沒設定時說去哪裡設；設定了就顯示上次查詢時間、可以立即查 -->
        <n-alert v-if="ext && !ext.configured" type="info" :bordered="false" size="small" style="margin-bottom: 10px"
                 data-testid="surface-ext-hint">
          {{ t("surface.ext_not_configured") }}
        </n-alert>
        <n-space v-else-if="ext" align="center" style="margin-bottom: 10px" data-testid="surface-ext-bar">
          <n-select v-model:value="extFilter" :options="extStatusOptions" style="width: 190px" clearable
                    :placeholder="t('surface.ext_all_status')" />
          <span style="font-size: 12.5px; opacity: .7">
            {{ t("surface.ext_last_run") }}：{{ ext.last_run_at ? fmtDateTime(ext.last_run_at) : "—" }}
          </span>
          <n-button v-if="auth.me?.is_admin" size="small" :loading="extRunning" data-testid="surface-ext-run" @click="runExternal">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("surface.ext_run") }}
          </n-button>
        </n-space>
        <!-- 沒有 DNS 名稱的開口在這個視角看不到 → 明講筆數，避免被當成全部 -->
        <n-alert v-if="noFqdnCount" type="warning" :bordered="false" size="small"
                 style="margin-bottom: 10px">
          {{ t("surface.no_fqdn_note", { n: noFqdnCount }) }}
        </n-alert>
        <n-data-table :columns="fqdnCols" :data="fqdnShown" :loading="loading" size="small"
                      :scroll-x="fqdnScrollX" :row-key="(r: FqdnRow) => r.key"
                      :pagination="pg" :bordered="false" />
        <n-empty v-if="!loading && !fqdnShown.length" style="margin: 24px 0"
                 :description="t('surface.empty_fqdn')" />
      </n-tab-pane>
    </n-tabs>

    <!-- AI 分析：邊寫邊顯示；關掉對話框不會中斷，結果留著可以再看、可以一起匯出 -->
    <n-modal v-model:show="aiOpen" preset="card" :title="t('surface.ai_title')" style="width: min(820px, 96vw)"
             data-testid="surface-ai-modal">
      <div class="surface-ai-meta">
        {{ t("surface.ai_scope", { n: aiRows }) }}
        <span v-if="aiRunning"> · {{ t("surface.ai_running", { s: aiElapsed }) }}</span>
        <span v-else-if="aiModel"> · {{ t("surface.ai_model") }}：{{ aiModel }} · {{ aiElapsed }}s</span>
      </div>
      <n-alert v-if="aiError" type="error" :bordered="false" style="margin-bottom: 10px">{{ aiError }}</n-alert>
      <div v-if="aiText" class="surface-ai-body" data-testid="surface-ai-body" v-html="aiHtml" />
      <div v-else-if="aiRunning" class="surface-ai-meta">{{ t("surface.ai_waiting") }}</div>
      <div v-if="aiText && !aiRunning" class="surface-ai-meta" style="margin-top: 10px">{{ t("surface.ai_disclaimer") }}</div>
      <template #footer>
        <n-space justify="end">
          <n-button v-if="aiRunning" type="error" ghost @click="stopAi">
            <template #icon><n-icon><CancelIcon /></n-icon></template>{{ t("surface.ai_stop") }}
          </n-button>
          <n-button v-else :disabled="!aiAvailable" @click="runAi">
            <template #icon><n-icon><AiAuditIcon /></n-icon></template>{{ t("surface.ai_rerun") }}
          </n-button>
          <n-button @click="aiOpen = false">
            <template #icon><n-icon><CancelIcon /></n-icon></template>{{ t("common.close") }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </n-card>
</template>

<style scoped>
.surface-ai-meta { font-size: 12.5px; opacity: .7; margin-bottom: 8px; }
.surface-ai-body { line-height: 1.7; font-size: 14px; }
.surface-ai-body :deep(h2) { font-size: 15px; margin: 14px 0 6px; }
.surface-ai-body :deep(ul) { padding-left: 20px; margin: 4px 0; }
</style>
