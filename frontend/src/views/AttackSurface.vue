<script setup lang="ts">
/**
 * 對外開放服務盤點：從外面可達的服務，每項配 IPAM 身分。
 *
 * 異常偵測的「對外曝險」是抓問題；這一頁是**盤點**——資安稽核第一個要的東西。
 * 只列明確可判定的開口；未登錄的目標用紅色標出（對外開口指向不明主機，本身就是紅旗）。
 * 欄位拆開（IP／埠／主機名稱／類型／名稱／防火牆各自獨立）才能排序與比對；
 * 欄位顯示走全站的欄位偏好，來源可依防火牆廠牌篩選。
 */
import { computed, onMounted, ref, h } from "vue";
import {
  NCard, NSpace, NIcon, NTag, NDataTable, NEmpty, NAlert, NButton, NSelect,
  useMessage, type DataTableColumns,
} from "naive-ui";
import { useI18n } from "vue-i18n";
import { apiClient, apiErrMsg } from "@/api/client";
import { FirewallIcon, RefreshIcon } from "@/icons";
import { autoSort } from "@/composables/useTableSort";
import { useTablePagination } from "@/composables/useTablePagination";
import { useColumnPrefs } from "@/composables/useColumnPrefs";
import ColumnPicker from "@/components/ColumnPicker.vue";

const { t } = useI18n();
const msg = useMessage();
const pg = useTablePagination();

interface Entry {
  via: string; source: string; firewall: string | null; name: string;
  protocol: string | null; port: number | string | null; descr: string;
  identity: { registered: boolean; ip?: string; hostname?: string | null;
              status?: string | null; subnet?: string | null;
              customer?: string | null; wazuh?: string | null };
}
const items = ref<Entry[]>([]);
const note = ref("");
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const { data } = await apiClient.get("/api/v1/anomalies/attack-surface");
    items.value = data.items ?? [];
    note.value = data.note ?? "";
  } catch (e: any) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}
onMounted(load);

// 來源篩選：清單由資料動態產生（有 manual 或未來新增廠牌時不必改這裡）
const sourceFilter = ref<string | null>(null);
const sourceOptions = computed(() => {
  const seen = [...new Set(items.value.map((i) => i.source))].sort();
  return [{ label: t("surface.all_sources"), value: "__all__" },
          ...seen.map((s) => ({ label: s, value: s }))];
});
const shown = computed(() =>
  (!sourceFilter.value || sourceFilter.value === "__all__")
    ? items.value
    : items.value.filter((i) => i.source === sourceFilter.value));

const ALL_KEYS = ["ip", "port", "hostname", "via", "name", "firewall",
                  "protocol", "owner", "wazuh", "status", "descr"];
const { visibleKeys, setVisible, reset } = useColumnPrefs("attack_surface", ALL_KEYS, ALL_KEYS);
const pickerCols = computed(() => ALL_KEYS.map((k) => ({ key: k, label: t(`surface.col_${k}`) })));

const allCols: Record<string, any> = {
  ip: { title: () => t("surface.col_ip"), key: "ip", width: 150,
    render: (r: Entry) => r.identity.registered
      ? r.identity.ip
      : h("span", null, ["?", h(NTag, { size: "tiny", type: "error", style: "margin-left:6px" },
          { default: () => t("surface.unregistered") })]) },
  port: { title: () => t("surface.col_port"), key: "port", width: 90,
    render: (r: Entry) => r.port ?? "—" },
  hostname: { title: () => t("surface.col_hostname"), key: "hostname", width: 160,
    render: (r: Entry) => r.identity.hostname || "—" },
  via: { title: () => t("surface.col_via"), key: "via", width: 90,
    render: (r: Entry) => r.via === "nat" ? "NAT" : t("surface.rule") },
  name: { title: () => t("surface.col_name"), key: "name", width: 220,
    ellipsis: { tooltip: true }, render: (r: Entry) => r.name || "—" },
  firewall: { title: () => t("surface.col_firewall"), key: "firewall", width: 180,
    render: (r: Entry) => h("span", null, [
      h(NTag, { size: "tiny", style: "margin-right:6px" }, { default: () => r.source }),
      r.firewall || "—",
    ]) },
  protocol: { title: () => t("surface.col_protocol"), key: "protocol", width: 90 },
  owner: { title: () => t("surface.col_owner"), key: "owner", width: 190,
    render: (r: Entry) => r.identity.customer || r.identity.subnet || "—" },
  wazuh: { title: "Wazuh", key: "wazuh", width: 110,
    render: (r: Entry) => r.identity.registered
      ? (r.identity.wazuh
          ? h(NTag, { size: "tiny", type: "success" }, { default: () => r.identity.wazuh })
          : h("span", { style: "opacity:.6" }, t("surface.no_agent")))
      : "—" },
  status: { title: () => t("surface.col_status"), key: "status", width: 120,
    render: (r: Entry) => r.identity.status || "—" },
  descr: { title: () => t("surface.col_descr"), key: "descr", ellipsis: { tooltip: true } },
};
const cols = computed<DataTableColumns<Entry>>(
  () => autoSort(ALL_KEYS.filter((k) => visibleKeys.value.includes(k)).map((k) => allCols[k])));
</script>

<template>
  <n-card :bordered="false">
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><FirewallIcon /></n-icon>
        <span>{{ t("surface.title") }}</span>
      </n-space>
    </template>
    <template #header-extra>
      <n-space align="center">
        <n-select v-model:value="sourceFilter" :options="sourceOptions"
                  size="small" style="width: 160px"
                  :placeholder="t('surface.all_sources')" clearable />
        <ColumnPicker :all="pickerCols" :visible="visibleKeys"
                      @update:visible="setVisible" @reset="reset" />
        <n-button size="small" :loading="loading" @click="load">
          <template #icon><n-icon><RefreshIcon /></n-icon></template>
          {{ t("common.refresh") }}
        </n-button>
      </n-space>
    </template>
    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("surface.hint") }}<template v-if="note"><br>{{ note }}</template>
    </n-alert>
    <n-data-table :columns="cols" :data="shown" :loading="loading" size="small"
                  :row-key="(r: Entry, i?: number) => `${r.name}-${r.port}-${i}`"
                  :pagination="pg" :bordered="false" />
    <n-empty v-if="!loading && !shown.length" style="margin: 24px 0"
             :description="t('surface.empty')" />
  </n-card>
</template>
