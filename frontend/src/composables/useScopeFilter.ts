/**
 * 依區段／子網路／單位篩選一份清單（「未裝 Agent 的 IP」用，Wazuh 與 OCS 兩頁共用）。
 *
 * 選項直接從資料裡出現過的值產生 —— 不用另外打 API，也不會列出篩了必定是空的選項。
 * 選了區段之後，子網路選單只列那個區段底下的。
 */
import { computed, ref, watch, type Ref } from "vue";

export interface ScopedRow {
  subnet_id?: string | null;
  subnet_cidr?: string | null;
  section_id?: string | null;
  section_name?: string | null;
  customer_id?: string | null;
  customer_name?: string | null;
}

type Opt = { label: string; value: string };

function distinct<T extends ScopedRow>(rows: T[], id: keyof ScopedRow, label: keyof ScopedRow): Opt[] {
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

  const sectionOpts = computed(() => distinct(rows.value, "section_id", "section_name"));
  const subnetOpts = computed(() => distinct(
    section.value ? rows.value.filter((r) => r.section_id === section.value) : rows.value,
    "subnet_id", "subnet_cidr"));
  const customerOpts = computed(() => distinct(rows.value, "customer_id", "customer_name"));
  // 換了區段、原本選的子網路不在新區段裡 → 清掉，免得篩出一片空白又看不出原因
  watch(section, () => {
    if (subnet.value && !subnetOpts.value.some((o) => o.value === subnet.value)) subnet.value = null;
  });

  const filtered = computed(() => rows.value.filter((r) =>
    (!section.value || r.section_id === section.value)
    && (!subnet.value || r.subnet_id === subnet.value)
    && (!customer.value || r.customer_id === customer.value)));
  const active = computed(() => !!(section.value || subnet.value || customer.value));

  return { section, subnet, customer, sectionOpts, subnetOpts, customerOpts, filtered, active };
}
