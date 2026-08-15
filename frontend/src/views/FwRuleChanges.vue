<script setup lang="ts">
/**
 * 防火牆規則異動歷史（異動偵測快照）。
 *
 * 異動通知只給摘要，並說「細節到快照裡看」—— 這一頁就是那個「快照裡」。
 * 沒有這頁的話，通知等於指向一個不存在的地方。admin 限定（規則內容屬純管理資料）。
 */
import { onMounted, ref, h } from "vue";
import {
  NCard, NSpace, NIcon, NTag, NDataTable, NEmpty, NAlert, NButton,
  useMessage, type DataTableColumns,
} from "naive-ui";
import { useI18n } from "vue-i18n";
import { apiClient, apiErrMsg } from "@/api/client";
import { fmtDateTime } from "@/utils/datetime";
import { FirewallIcon, RefreshIcon } from "@/icons";
import { autoSort } from "@/composables/useTableSort";

const { t } = useI18n();
const msg = useMessage();

interface Change {
  id: string; source_type: string; instance_name: string; taken_at: string;
  rule_count: number; is_baseline: boolean;
  diff: { added: any[]; removed: any[]; changed: any[] } | null;
}
const items = ref<Change[]>([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const { data } = await apiClient.get("/api/v1/anomalies/fw-rule-changes");
    items.value = data.items ?? [];
  } catch (e: any) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}
onMounted(load);

function ruleLine(r: any): string {
  const port = r.dst_port ? `:${r.dst_port}` : "";
  const descr = r.descr ? `（${r.descr}）` : "";
  return `${r.action || "?"} ${r.src || "any"} → ${r.dst || "any"}${port}${descr}`;
}

/** diff 攤成可讀清單；規則描述是不可信文字，一律以純文字 render（不進 v-html）。 */
function renderDiff(row: Change) {
  if (row.is_baseline) {
    return h("span", { style: "opacity:.65" }, t("fw_changes.baseline"));
  }
  const d = row.diff!;
  const blocks: any[] = [];
  const mk = (label: string, color: string, rows: string[]) => rows.length
    ? h("div", { style: "margin: 2px 0" }, [
        h("span", { style: `color:${color};font-weight:600;margin-right:6px` }, label),
        h("div", { style: "display:flex;flex-direction:column;gap:2px;font-size:12.5px" },
          rows.map((x) => h("div", null, x))),
      ]) : null;
  blocks.push(mk("＋", "#d03050", (d.added ?? []).map(ruleLine)));
  blocks.push(mk("－", "#888", (d.removed ?? []).map(ruleLine)));
  blocks.push(mk("Δ", "#f0a020", (d.changed ?? []).map(
    (c: any) => `${c.descr || c.key}：${(c.fields ?? []).map(
      (f: string) => `${f} ${c.old?.[f] ?? ""} → ${c.new?.[f] ?? ""}`).join("、")}`)));
  return h("div", null, blocks.filter(Boolean));
}

const cols: DataTableColumns<Change> = autoSort([
  { title: t("fw_changes.when"), key: "taken_at", width: 170,
    render: (r) => fmtDateTime(r.taken_at) },
  { title: t("fw_changes.firewall"), key: "instance_name", width: 200,
    render: (r) => h("span", null, [
      h(NTag, { size: "tiny", style: "margin-right:6px" }, { default: () => r.source_type }),
      r.instance_name,
    ]) },
  { title: t("fw_changes.rules"), key: "rule_count", width: 90 },
  { title: t("fw_changes.diff"), key: "diff", render: (r) => renderDiff(r) },
]);
</script>

<template>
  <n-card :bordered="false">
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><FirewallIcon /></n-icon>
        <span>{{ t("fw_changes.title") }}</span>
      </n-space>
    </template>
    <template #header-extra>
      <n-button size="small" :loading="loading" @click="load">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
    </template>
    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("fw_changes.hint") }}
    </n-alert>
    <n-data-table :columns="cols" :data="items" :loading="loading" size="small"
                  :row-key="(r: Change) => r.id" :bordered="false" />
    <n-empty v-if="!loading && !items.length" style="margin: 24px 0"
             :description="t('fw_changes.empty')" />
  </n-card>
</template>
