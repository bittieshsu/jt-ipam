/**
 * 依區段／子網路／單位／上線狀態篩選一份清單（「未裝 Agent 的 IP」用，Wazuh 與 OCS 兩頁共用）。
 *
 * 選項直接從資料裡出現過的值產生 —— 不用另外打 API，也不會列出篩了必定是空的選項。
 * 選了區段之後，子網路選單只列那個區段底下的。
 * 上線狀態用 IP 清單燈號的同一套規則即時算（classifyAddressLiveness），兩邊不會講不一樣。
 */
import { computed, ref, watch, type Ref } from "vue";
import { classifyAddressLiveness, type LivenessKind } from "@/composables/useLivenessSettings";

export interface ScopedRow {
  subnet_id?: string | null;
  subnet_cidr?: string | null;
  section_id?: string | null;
  section_name?: string | null;
  customer_id?: string | null;
  customer_name?: string | null;
  // 上線判斷吃的欄位（後端 agent_scope.annotate_scope 帶上）
  last_seen_scanner?: string | null;
  last_seen_librenms?: string | null;
  last_seen_arp?: string | null;
  last_seen_wazuh?: string | null;
  last_seen_zabbix?: string | null;
  arp_seen?: Record<string, string> | null;
  exclude_from_ping?: boolean | null;
  subnet_scan_enabled?: boolean | null;
}

const STATUS_ORDER: LivenessKind[] = ["online", "stale", "offline", "unknown"];

type Opt = { label: string; value: string };

type ScopeKey = "subnet_id" | "subnet_cidr" | "section_id" | "section_name" | "customer_id" | "customer_name";

function distinct<T extends ScopedRow>(rows: T[], id: ScopeKey, label: ScopeKey): Opt[] {
  const m = new Map<string, string>();
  for (const r of rows) {
    const v = r[id];
    if (v && !m.has(v)) m.set(v, (r[label] as string) || v);
  }
  return [...m].map(([value, l]) => ({ value, label: l }))
    .sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true }));
}

export function useScopeFilter<T extends ScopedRow>(rows: Ref<T[]>) {
  const section = ref<string | null>(null);
  const subnet = ref<string | null>(null);
  const customer = ref<string | null>(null);
  const status = ref<LivenessKind | null>(null);

  const sectionOpts = computed(() => distinct(rows.value, "section_id", "section_name"));
  const subnetOpts = computed(() => distinct(
    section.value ? rows.value.filter((r) => r.section_id === section.value) : rows.value,
    "subnet_id", "subnet_cidr"));
  const customerOpts = computed(() => distinct(rows.value, "customer_id", "customer_name"));
  const statusOf = (r: T): LivenessKind => classifyAddressLiveness(r);
  // label 就是狀態代碼，由畫面翻譯（這裡沒有 i18n）
  const statusOpts = computed(() => {
    const seen = new Set(rows.value.map(statusOf));
    return STATUS_ORDER.filter((k) => seen.has(k)).map((k) => ({ value: k, label: k }));
  });
  // 換了區段、原本選的子網路不在新區段裡 → 清掉，免得篩出一片空白又看不出原因
  watch(section, () => {
    if (subnet.value && !subnetOpts.value.some((o) => o.value === subnet.value)) subnet.value = null;
  });

  const filtered = computed(() => rows.value.filter((r) =>
    (!section.value || r.section_id === section.value)
    && (!subnet.value || r.subnet_id === subnet.value)
    && (!customer.value || r.customer_id === customer.value)
    && (!status.value || statusOf(r) === status.value)));
  const active = computed(() => !!(section.value || subnet.value || customer.value || status.value));

  return { section, subnet, customer, status, sectionOpts, subnetOpts, customerOpts, statusOpts,
           filtered, active };
}
