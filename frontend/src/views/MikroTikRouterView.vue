<script setup lang="ts">
/**
 * MikroTik RouterOS 檢視（唯讀）：防火牆規則 / address-list。
 * 資料由 MikroTik 整合同步進來；本頁不呼叫路由器、也不修改任何設定。
 *
 * 規則依 `position` 排序，**而且不提供改排序** —— RouterOS 由上而下比對、
 * 第一條命中就決定結果，用別的順序看等於看不出行為。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NCard, NDataTable, NSpace, NSelect, NIcon, NEmpty, NTabs, NTabPane, NTag, NInput,
  useMessage, type DataTableColumns,
} from "naive-ui";
import { FirewallIcon } from "@/icons";
import {
  listMikroTik, listMikroTikRules, listMikroTikAddressLists,
  type MikroTikRouter, type MikroTikRule, type MikroTikAddressListEntry,
} from "@/api/mikrotik";
import { autoSort } from "@/composables/useTableSort";
import { apiErrMsg } from "@/api/client";

const { t } = useI18n();
const msg = useMessage();

const routers = ref<MikroTikRouter[]>([]);
const routerId = ref<string | null>(null);
const table = ref<string | null>(null);
const listFilter = ref("");
const rules = ref<MikroTikRule[]>([]);
const entries = ref<MikroTikAddressListEntry[]>([]);
const loading = ref(false);

const routerOptions = computed(() => routers.value.map((r) => ({ label: r.name, value: r.id })));
const tableOptions = computed(() => ["filter", "nat", "mangle"].map((v) => ({ label: v, value: v })));

const filteredEntries = computed(() => {
  const q = listFilter.value.trim().toLowerCase();
  if (!q) return entries.value;
  return entries.value.filter((e) =>
    e.list_name.toLowerCase().includes(q) || e.address.toLowerCase().includes(q));
});

async function loadRouters() {
  try {
    routers.value = (await listMikroTik()).items;
    if (!routerId.value && routers.value.length) routerId.value = routers.value[0].id;
  } catch (e) { msg.error(apiErrMsg(e)); }
}

async function loadData() {
  if (!routerId.value) return;
  loading.value = true;
  try {
    [rules.value, entries.value] = await Promise.all([
      listMikroTikRules(routerId.value, table.value ?? undefined),
      listMikroTikAddressLists(routerId.value),
    ]);
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}

watch([routerId, table], () => { void loadData(); });
onMounted(async () => { await loadRouters(); await loadData(); });

const ruleCols = computed<DataTableColumns<MikroTikRule>>(() => autoSort([
  { title: t("mikrotik.col_table"), key: "table_name", width: 90 },
  { title: t("mikrotik.col_chain"), key: "chain", width: 110,
    render: (r) => r.chain ?? "—" },
  { title: "#", key: "position", width: 60 },
  { title: t("fortigate.col_action"), key: "action", width: 110,
    render: (r) => r.action ?? "—" },
  { title: t("common.status"), key: "disabled", width: 90,
    render: (r) => r.disabled ? t("common.disabled") : t("common.enabled") },
  { title: t("fortigate.col_src"), key: "src_address", minWidth: 150,
    ellipsis: { tooltip: true },
    render: (r) => [r.src_address, r.src_port].filter(Boolean).join(":") || "—" },
  { title: t("fortigate.col_dst"), key: "dst_address", minWidth: 150,
    ellipsis: { tooltip: true },
    render: (r) => [r.dst_address, r.dst_port].filter(Boolean).join(":") || "—" },
  { title: t("mikrotik.col_proto"), key: "protocol", width: 90,
    render: (r) => r.protocol ?? "—" },
  { title: t("mikrotik.col_iface"), key: "in_interface", minWidth: 140,
    ellipsis: { tooltip: true },
    render: (r) => `${r.in_interface ?? "—"} → ${r.out_interface ?? "—"}` },
  { title: t("mikrotik.col_to"), key: "to_addresses", minWidth: 140,
    ellipsis: { tooltip: true },
    render: (r) => [r.to_addresses, r.to_ports].filter(Boolean).join(":") || "—" },
  { title: t("sections.description"), key: "comment", minWidth: 140,
    ellipsis: { tooltip: true }, render: (r) => r.comment ?? "—" },
]));

const entryCols = computed<DataTableColumns<MikroTikAddressListEntry>>(() => autoSort([
  { title: t("mikrotik.col_list"), key: "list_name", minWidth: 160,
    ellipsis: { tooltip: true } },
  { title: t("fortigate.col_value"), key: "address", minWidth: 180,
    ellipsis: { tooltip: true } },
  // 動態條目是規則自己加進去的（例如防掃描），不是人設定的 —— 分開看很重要
  { title: t("common.type"), key: "dynamic", width: 110,
    render: (r) => r.dynamic ? t("mikrotik.dynamic") : t("mikrotik.static") },
  { title: t("mikrotik.col_timeout"), key: "timeout", width: 120,
    render: (r) => r.timeout ?? "—" },
  { title: t("sections.description"), key: "comment", minWidth: 140,
    ellipsis: { tooltip: true }, render: (r) => r.comment ?? "—" },
]));
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><FirewallIcon /></n-icon>
        <span>{{ t("mikrotik.view_title") }}</span>
        <n-tag size="small" type="warning" :bordered="false">Beta</n-tag>
      </n-space>
    </template>

    <n-space style="margin-bottom: 12px" align="center">
      <n-select v-model:value="routerId" :options="routerOptions" style="width: 220px"
                :placeholder="t('mikrotik.pick_router')" />
      <n-select v-model:value="table" :options="tableOptions" clearable style="width: 160px"
                :placeholder="t('mikrotik.all_tables')" />
      <n-input v-model:value="listFilter" clearable style="width: 200px"
               :placeholder="t('mikrotik.filter_lists')" />
    </n-space>

    <n-empty v-if="!routers.length" :description="t('mikrotik.none_configured')" />
    <n-tabs v-else type="line">
      <n-tab-pane name="rules" :tab="t('mikrotik.rules')">
        <n-data-table :columns="ruleCols" :data="rules" :loading="loading"
                      :bordered="false" :scroll-x="1400" />
      </n-tab-pane>
      <n-tab-pane name="lists" :tab="t('mikrotik.address_lists')">
        <n-data-table :columns="entryCols" :data="filteredEntries" :loading="loading"
                      :bordered="false" :scroll-x="900"
                      :pagination="{ pageSize: 50, showSizePicker: false }" />
      </n-tab-pane>
    </n-tabs>
  </n-card>
</template>
