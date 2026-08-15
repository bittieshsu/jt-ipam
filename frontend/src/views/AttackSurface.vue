<script setup lang="ts">
/**
 * 對外開放服務清單：從外面可達的服務，每項配 IPAM 身分。
 *
 * 異常偵測的「對外曝險」是抓問題；這一頁是**清單**——資安稽核第一個要的東西。
 * 只列明確可判定的開口；未登錄的目標用紅色標出（對外開口指向不明主機，本身就是警訊）。
 * 欄位拆開（IP／埠／主機名稱／類型／名稱／防火牆各自獨立）才能排序與比對；
 * 欄位顯示走全站的欄位偏好，來源可依防火牆廠牌篩選。
 */
import { computed, onMounted, ref, h } from "vue";
import {
  NCard, NSpace, NIcon, NTag, NDataTable, NEmpty, NAlert, NButton, NSelect, NInput,
  useMessage, type DataTableColumns,
} from "naive-ui";
import { useI18n } from "vue-i18n";
import { apiClient, apiErrMsg } from "@/api/client";
import { FirewallIcon, RefreshIcon, SearchIcon } from "@/icons";
import { autoSort } from "@/composables/useTableSort";
import { useTablePagination } from "@/composables/useTablePagination";
import { useColumnPrefs } from "@/composables/useColumnPrefs";
import ColumnPicker from "@/components/ColumnPicker.vue";
import { useRouter } from "vue-router";
import { useEntityLinks } from "@/composables/useEntityLinks";

const { t } = useI18n();
const msg = useMessage();
const pg = useTablePagination();
const links = useEntityLinks(useRouter());

interface Entry {
  via: string; source: string; firewall: string | null; name: string;
  protocol: string | null; port: number | string | null; descr: string;
  identity: { registered: boolean; ip?: string; ip_id?: string; hostname?: string | null;
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

// 篩選：選項一律由資料動態產生（新增廠牌／協定／單位都不必改程式）。
// 各下拉與搜尋框是「且」的關係，疊加過濾。
const sourceFilter = ref<string | null>(null);
const viaFilter = ref<string | null>(null);
const protoFilter = ref<string | null>(null);
const statusFilter = ref<string | null>(null);
const ownerFilter = ref<string | null>(null);
const searchText = ref("");

function opts(values: (string | null | undefined)[], allLabel: string) {
  const seen = [...new Set(values.filter((v): v is string => !!v))].sort();
  return [{ label: allLabel, value: "__all__" },
          ...seen.map((v) => ({ label: v, value: v }))];
}
const sourceOptions = computed(() => opts(items.value.map((i) => i.source), t("surface.all_sources")));
const viaOptions = computed(() => [
  { label: t("surface.all_via"), value: "__all__" },
  { label: "NAT", value: "nat" }, { label: t("surface.rule"), value: "rule" },
]);
const protoOptions = computed(() => opts(items.value.map((i) => i.protocol), t("surface.all_proto")));
const statusOptions = computed(() => opts(items.value.map((i) => i.identity.status), t("surface.all_status")));
const ownerOptions = computed(() => opts(items.value.map((i) => i.identity.customer), t("surface.all_owner")));

// NAT ↔ 規則配對：pfSense/OPNsense 的埠轉發常帶一條關聯放行規則。
// 同目標 IP＋同埠同時存在 NAT 列與規則列 → 兩列都標「配對」，一眼看出它們是一組。
// 純資料比對，不猜語意；對不上的（例如規則沒寫埠）就不標。
const pairedKeys = computed(() => {
  const nat = new Set<string>(), rule = new Set<string>();
  for (const i of items.value) {
    const key = `${i.identity.ip ?? "?"}:${i.port ?? ""}`;
    (i.via === "nat" ? nat : rule).add(key);
  }
  return new Set([...nat].filter((k) => rule.has(k)));
});
const isPaired = (r: Entry) => pairedKeys.value.has(`${r.identity.ip ?? "?"}:${r.port ?? ""}`);

const pick = (f: string | null, v: string | null | undefined) =>
  !f || f === "__all__" || (v ?? "") === f;
const shown = computed(() => {
  let out = items.value.filter((i) =>
    pick(sourceFilter.value, i.source)
    && pick(viaFilter.value, i.via)
    && pick(protoFilter.value, i.protocol)
    && pick(statusFilter.value, i.identity.status)
    && pick(ownerFilter.value, i.identity.customer));
  const q = searchText.value.trim().toLowerCase();
  if (q) {
    out = out.filter((i) =>
      [i.identity.ip, i.identity.hostname, i.name, i.descr, String(i.port ?? ""),
       i.firewall, i.identity.customer, i.identity.subnet]
        .some((v) => (v ?? "").toString().toLowerCase().includes(q)));
  }
  return out;
});

const ALL_KEYS = ["ip", "port", "hostname", "via", "name", "firewall",
                  "protocol", "owner", "wazuh", "status", "descr"];
const { visibleKeys, setVisible, reset } = useColumnPrefs("attack_surface", ALL_KEYS, ALL_KEYS);
const pickerCols = computed(() => ALL_KEYS.map((k) => ({ key: k, label: t(`surface.col_${k}`) })));

const allCols: Record<string, any> = {
  ip: { title: () => t("surface.col_ip"), key: "ip", width: 150,
    render: (r: Entry) => r.identity.registered
      // IPAM 有這筆 → 點過去 IP 卡片（全站同一套 entity link）
      ? (r.identity.ip_id
          ? links.ipById(r.identity.ip_id, r.identity.ip ?? "")
          : r.identity.ip)
      : h("span", null, ["?", h(NTag, { size: "tiny", type: "error", style: "margin-left:6px" },
          { default: () => t("surface.unregistered") })]) },
  port: { title: () => t("surface.col_port"), key: "port", width: 90,
    render: (r: Entry) => r.port ?? "—" },
  hostname: { title: () => t("surface.col_hostname"), key: "hostname", width: 160,
    render: (r: Entry) => r.identity.hostname || "—" },
  via: { title: () => t("surface.col_via"), key: "via", width: 130,
    render: (r: Entry) => h("span", { style: "display:inline-flex;align-items:center;gap:6px" }, [
      r.via === "nat" ? "NAT" : t("surface.rule"),
      isPaired(r) ? h(NTag, { size: "tiny", type: "info", bordered: false,
                              title: t("surface.paired_tip") },
                      { default: () => t("surface.paired") }) : null,
    ]) },
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
  status: { title: () => t("surface.col_status"), key: "status", width: 130,
    render: (r: Entry) => {
      const raw = r.identity.status;
      if (!raw) return "—";
      const online = raw.toLowerCase().startsWith("online");
      const src = (raw.match(/\(([^)]+)\)/)?.[1]) ?? "";
      return h("span", { style: "display:inline-flex;align-items:center;gap:6px" }, [
        h("i", { style: `width:8px;height:8px;border-radius:50%;flex:none;background:${online ? "#22c55e" : "#ef4444"}` }),
        online ? t("surface.st_online") : t("surface.st_offline"),
        src ? h("span", { style: "opacity:.55;font-size:11.5px" }, src) : null,
      ]);
    } },
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
    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("surface.hint") }}<template v-if="note"><br>{{ note }}</template>
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
      <n-select v-model:value="viaFilter" :options="viaOptions" style="width: 120px"
                :placeholder="t('surface.all_via')" clearable />
      <n-select v-model:value="protoFilter" :options="protoOptions" style="width: 120px"
                :placeholder="t('surface.all_proto')" clearable />
      <n-select v-model:value="statusFilter" :options="statusOptions" style="width: 140px"
                :placeholder="t('surface.all_status')" clearable />
      <n-select v-model:value="ownerFilter" :options="ownerOptions" style="width: 170px"
                :placeholder="t('surface.all_owner')" clearable />
      <ColumnPicker :all="pickerCols" :visible="visibleKeys"
                    @update:visible="setVisible" @reset="reset" />
      <n-button size="small" :loading="loading" @click="load">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
    </n-space>
    <n-data-table :columns="cols" :data="shown" :loading="loading" size="small"
                  :row-key="(r: Entry, i?: number) => `${r.name}-${r.port}-${i}`"
                  :pagination="pg" :bordered="false" />
    <n-empty v-if="!loading && !shown.length" style="margin: 24px 0"
             :description="t('surface.empty')" />
  </n-card>
</template>
