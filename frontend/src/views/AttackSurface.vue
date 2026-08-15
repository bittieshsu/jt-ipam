<script setup lang="ts">
/**
 * 對外攻擊面盤點：從外面可達的 IP:port，每項配 IPAM 身分。
 *
 * 異常偵測的「對外曝險」是抓問題；這一頁是**盤點**——資安稽核第一個要的東西。
 * 只列明確可判定的開口；未登錄的目標用紅色標出（對外開口指向不明主機，本身就是紅旗）。
 */
import { onMounted, ref, h } from "vue";
import {
  NCard, NSpace, NIcon, NTag, NDataTable, NEmpty, NAlert, NButton,
  useMessage, type DataTableColumns,
} from "naive-ui";
import { useI18n } from "vue-i18n";
import { apiClient, apiErrMsg } from "@/api/client";
import { FirewallIcon, RefreshIcon } from "@/icons";
import { autoSort } from "@/composables/useTableSort";
import { useTablePagination } from "@/composables/useTablePagination";

const { t } = useI18n();
const msg = useMessage();
const pg = useTablePagination();

interface Entry {
  via: string; source: string; name: string; protocol: string | null;
  port: number | string | null; descr: string;
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

const cols: DataTableColumns<Entry> = autoSort([
  { title: t("surface.target"), key: "identity", width: 260,
    render: (r) => r.identity.registered
      ? h("span", null, [
          `${r.identity.ip}${r.port ? ":" + r.port : ""}`,
          h("span", { style: "opacity:.65;margin-left:6px" },
            r.identity.hostname ? `（${r.identity.hostname}）` : ""),
        ])
      : h("span", null, [
          `${r.port ? "?:" + r.port : "?"}`,
          h(NTag, { size: "tiny", type: "error", style: "margin-left:6px" },
            { default: () => t("surface.unregistered") }),
        ]) },
  { title: t("surface.via"), key: "via", width: 200,
    render: (r) => h("span", null, [
      h(NTag, { size: "tiny", style: "margin-right:6px" }, { default: () => r.source }),
      r.via === "nat" ? `NAT｜${r.name}` : `${t("surface.rule")}｜${r.name}`,
    ]) },
  { title: t("surface.protocol"), key: "protocol", width: 90 },
  { title: t("surface.owner"), key: "_owner", width: 200,
    render: (r) => r.identity.customer || r.identity.subnet || "—" },
  { title: "Wazuh", key: "_wazuh", width: 110,
    render: (r) => r.identity.registered
      ? (r.identity.wazuh
          ? h(NTag, { size: "tiny", type: "success" }, { default: () => r.identity.wazuh })
          : h("span", { style: "opacity:.6" }, t("surface.no_agent")))
      : "—" },
  { title: t("surface.status"), key: "_st", width: 130,
    render: (r) => r.identity.status || "—" },
  { title: t("surface.descr"), key: "descr", ellipsis: { tooltip: true } },
]);
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
      <n-button size="small" :loading="loading" @click="load">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
    </template>
    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("surface.hint") }}<template v-if="note"><br>{{ note }}</template>
    </n-alert>
    <n-data-table :columns="cols" :data="items" :loading="loading" size="small"
                  :row-key="(r: Entry, i?: number) => `${r.name}-${r.port}-${i}`"
                  :pagination="pg" :bordered="false" />
    <n-empty v-if="!loading && !items.length" style="margin: 24px 0"
             :description="t('surface.empty')" />
  </n-card>
</template>
