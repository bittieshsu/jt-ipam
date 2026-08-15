<script setup lang="ts">
/**
 * 防火牆規則異動歷史（異動偵測快照）。
 *
 * 異動通知只給摘要，並說「細節到快照裡看」—— 這一頁就是那個「快照裡」。
 * 沒有這頁的話，通知等於指向一個不存在的地方。admin 限定（規則內容屬純管理資料）。
 */
import { onMounted, ref, h } from "vue";
import {
  NCard, NSpace, NIcon, NTag, NDataTable, NEmpty, NAlert, NButton, NModal, NInput,
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
  ack: { at: string; note: string } | null;
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
  // 每一列＝一條規則：小型彩色標籤（新增/移除/修改）與規則文字**同一行**——
  // 原本紅色「＋」自己佔一行、規則文字掉到下一行，看起來像排版壞掉（使用者截圖）。
  const line = (tagText: string, tagType: "error" | "default" | "warning", text: string) =>
    h("div", { style: "display:flex;align-items:baseline;gap:6px;font-size:12.5px;line-height:1.7" }, [
      h(NTag, { size: "tiny", type: tagType, style: "flex:none" }, { default: () => tagText }),
      h("span", null, text),
    ]);
  const rows: any[] = [];
  for (const r of (d.added ?? [])) rows.push(line(t("fw_changes.d_add"), "error", ruleLine(r)));
  for (const r of (d.removed ?? [])) rows.push(line(t("fw_changes.d_del"), "default", ruleLine(r)));
  for (const c of (d.changed ?? [])) rows.push(line(t("fw_changes.d_chg"), "warning",
    `${c.descr || c.key}：${(c.fields ?? []).map(
      (f: string) => `${f} ${c.old?.[f] ?? ""} → ${c.new?.[f] ?? ""}`).join("、")}`));
  return h("div", { style: "display:flex;flex-direction:column;gap:3px;padding:4px 0" }, rows);
}

/** AI 解讀：偵測是確定性的，解讀層按需觸發 —— 帶上目標位址的全系統整合證據。 */
const aiBusy = ref<string | null>(null);
const aiResult = ref<{ card: string; disclaimer: string } | null>(null);
async function analyze(row: Change) {
  aiBusy.value = row.id;
  try {
    const { data } = await apiClient.post(`/api/v1/anomalies/fw-rule-changes/${row.id}/analyze`);
    aiResult.value = data;
  } catch (e: any) { msg.error(apiErrMsg(e)); }
  finally { aiBusy.value = null; }
}

/** 認領：把異動標記為「已知變更＋說明」。沒被認領的異動＝稽核上無人說明的變更。 */
const ackTarget = ref<Change | null>(null);
const ackNote = ref("");
const ackBusy = ref(false);
async function submitAck() {
  if (!ackTarget.value) return;
  ackBusy.value = true;
  try {
    await apiClient.post(`/api/v1/anomalies/fw-rule-changes/${ackTarget.value.id}/ack`,
                         { note: ackNote.value.trim() });
    ackTarget.value = null; ackNote.value = "";
    await load();
  } catch (e: any) { msg.error(apiErrMsg(e)); }
  finally { ackBusy.value = false; }
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
  { title: t("fw_changes.ack_col"), key: "_ack", width: 150,
    render: (r) => r.is_baseline ? null : (r.ack
      ? h("span", { style: "font-size:12px;opacity:.75" },
          `✓ ${t("fw_changes.acked")}${r.ack.note ? "：" + r.ack.note.slice(0, 40) : ""}`)
      : h(NButton, { size: "tiny", secondary: true,
                     onClick: () => { ackTarget.value = r; ackNote.value = ""; } },
          { default: () => t("fw_changes.ack_btn") })) },
  { title: t("fw_changes.ai"), key: "_ai", width: 110,
    render: (r) => r.is_baseline ? null : h(NButton, {
      size: "tiny", secondary: true, loading: aiBusy.value === r.id,
      onClick: () => analyze(r),
    }, { default: () => t("fw_changes.ai_btn") }) },
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
    <!-- 控制列：自標題列搬到內文最上方 -->
    <n-space align="center" justify="end" style="margin-bottom: 10px">
      <n-button size="small" :loading="loading" @click="load">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
    </n-space>
    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("fw_changes.hint") }}
    </n-alert>
    <n-data-table :columns="cols" :data="items" :loading="loading" size="small"
                  :row-key="(r: Change) => r.id" :bordered="false" />
    <n-empty v-if="!loading && !items.length" style="margin: 24px 0"
             :description="t('fw_changes.empty')" />
  </n-card>

  <!-- 認領：合規證據鏈（誰確認了這筆變更、為什麼） -->
  <n-modal :show="!!ackTarget" preset="card" style="width: 460px; max-width: 92vw"
           :title="t('fw_changes.ack_title')"
           @update:show="(v: boolean) => { if (!v) ackTarget = null; }">
    <n-input v-model:value="ackNote" type="textarea" :rows="3"
             :placeholder="t('fw_changes.ack_ph')" />
    <template #footer>
      <n-space justify="end">
        <n-button @click="ackTarget = null">{{ t("common.cancel") }}</n-button>
        <n-button type="primary" :loading="ackBusy" @click="submitAck">
          {{ t("common.confirm") }}
        </n-button>
      </n-space>
    </template>
  </n-modal>

  <!-- AI 解讀卡：模型輸出以純文字呈現（pre-wrap），不進 v-html -->
  <n-modal :show="!!aiResult" preset="card" style="width: 560px; max-width: 94vw"
           :title="t('fw_changes.ai_title')"
           @update:show="(v: boolean) => { if (!v) aiResult = null; }">
    <n-alert type="warning" :bordered="false" style="margin-bottom: 10px">
      {{ aiResult?.disclaimer }}
    </n-alert>
    <div style="white-space: pre-wrap; font-size: 13.5px; line-height: 1.7">{{ aiResult?.card }}</div>
  </n-modal>
</template>
